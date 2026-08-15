from __future__ import annotations

import json
import os
import hashlib
import threading
import traceback
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from cli.models import AnalystType, AssetType
from tradingagents.default_config import DEFAULT_CONFIG


ANALYSTS = ("market", "social", "news", "fundamentals")
CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")
USER_CONFIG_KEYS = ("output_language", "max_debate_rounds", "max_risk_discuss_rounds")
SYSTEM_CONFIG_KEYS = (
    "llm_provider",
    "deep_think_llm",
    "quick_think_llm",
    "backend_url",
    "temperature",
    "llm_max_retries",
    "llm_timeout",
    "checkpoint_enabled",
    "data_vendors",
    "tool_vendors",
    "results_dir",
    "data_cache_dir",
    "memory_log_path",
    "news_article_limit",
    "global_news_article_limit",
    "global_news_lookback_days",
)
SENSITIVE_ENV_KEYS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "MISTRAL_API_KEY",
    "MOONSHOT_API_KEY",
    "GROQ_API_KEY",
    "NVIDIA_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
    "FRED_API_KEY",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state_dir() -> Path:
    configured = os.environ.get("TRADINGAGENTS_WEB_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(DEFAULT_CONFIG["results_dir"]) / "web"


def redact_value(key: str, value: Any) -> Any:
    if value in (None, ""):
        return value
    lowered = key.lower()
    if "key" in lowered or "token" in lowered or "secret" in lowered:
        return "***configured***"
    return value


def normalize_ticker_symbol(ticker: str) -> str:
    try:
        from tradingagents.dataflows.symbol_utils import normalize_symbol

        return normalize_symbol(ticker)
    except Exception:
        return ticker.strip().upper()


def detect_asset_type_value(ticker: str) -> str:
    canonical = normalize_ticker_symbol(ticker)
    if canonical.endswith(CRYPTO_SUFFIXES):
        return AssetType.CRYPTO.value
    return AssetType.STOCK.value


def allowed_analysts_for_asset(asset_type: str) -> set[str]:
    analysts = {item.value for item in AnalystType}
    if asset_type == AssetType.CRYPTO.value:
        analysts.discard(AnalystType.FUNDAMENTALS.value)
    return analysts


def system_settings_snapshot(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return deployer-owned settings separately from user-run inputs."""
    cfg = config or DEFAULT_CONFIG
    settings = [
        {
            "key": key,
            "value": redact_value(key, cfg.get(key)),
            "source": "environment" if _env_controls_key(key) else "default/config",
            "scope": "system",
        }
        for key in SYSTEM_CONFIG_KEYS
    ]
    secrets = [
        {
            "key": key,
            "configured": bool(os.environ.get(key)),
            "value": "***configured***" if os.environ.get(key) else None,
            "scope": "system",
        }
        for key in SENSITIVE_ENV_KEYS
    ]
    deployment = {
        "public_url": os.environ.get("TRADINGAGENTS_WEB_PUBLIC_URL"),
        "admin_token_required": bool(os.environ.get("TRADINGAGENTS_WEB_ADMIN_TOKEN")),
        "state_dir": str(default_state_dir()),
        "scope": "system",
    }
    user_behavior_schema = {
        "fields": [
            "ticker",
            "trade_date",
            "asset_type",
            "selected_analysts",
            *USER_CONFIG_KEYS,
        ],
        "scope": "user_behavior",
    }
    return {
        "settings": settings,
        "secrets": secrets,
        "deployment": deployment,
        "user_behavior_schema": user_behavior_schema,
    }


def _env_controls_key(config_key: str) -> bool:
    env_map = {
        "llm_provider": "TRADINGAGENTS_LLM_PROVIDER",
        "deep_think_llm": "TRADINGAGENTS_DEEP_THINK_LLM",
        "quick_think_llm": "TRADINGAGENTS_QUICK_THINK_LLM",
        "backend_url": "TRADINGAGENTS_LLM_BACKEND_URL",
        "output_language": "TRADINGAGENTS_OUTPUT_LANGUAGE",
        "max_debate_rounds": "TRADINGAGENTS_MAX_DEBATE_ROUNDS",
        "max_risk_discuss_rounds": "TRADINGAGENTS_MAX_RISK_ROUNDS",
        "checkpoint_enabled": "TRADINGAGENTS_CHECKPOINT_ENABLED",
        "temperature": "TRADINGAGENTS_TEMPERATURE",
        "llm_max_retries": "TRADINGAGENTS_LLM_MAX_RETRIES",
        "llm_timeout": "TRADINGAGENTS_LLM_TIMEOUT",
    }
    env_name = env_map.get(config_key)
    return bool(env_name and os.environ.get(env_name))


@dataclass
class AnalysisRequest:
    ticker: str
    trade_date: str = field(default_factory=lambda: date.today().isoformat())
    asset_type: str | None = None
    selected_analysts: list[str] = field(default_factory=lambda: list(ANALYSTS))
    output_language: str | None = None
    max_debate_rounds: int | None = None
    max_risk_discuss_rounds: int | None = None
    note: str | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "AnalysisRequest":
        ticker = str(data.get("ticker", "")).strip()
        if not ticker:
            raise ValueError("ticker is required")
        requested = cls(
            ticker=normalize_ticker_symbol(ticker),
            trade_date=str(data.get("trade_date") or date.today().isoformat()),
            asset_type=data.get("asset_type") or None,
            selected_analysts=list(data.get("selected_analysts") or ANALYSTS),
            output_language=data.get("output_language") or None,
            max_debate_rounds=_optional_int(data.get("max_debate_rounds")),
            max_risk_discuss_rounds=_optional_int(data.get("max_risk_discuss_rounds")),
            note=data.get("note") or None,
        )
        requested.validate()
        return requested

    def validate(self) -> None:
        datetime.strptime(self.trade_date, "%Y-%m-%d")
        detected_asset = detect_asset_type_value(self.ticker)
        if self.asset_type is None:
            self.asset_type = detected_asset
        if self.asset_type not in {"stock", "crypto"}:
            raise ValueError("asset_type must be stock or crypto")
        allowed = allowed_analysts_for_asset(self.asset_type)
        selected = [item.lower() for item in self.selected_analysts]
        invalid = sorted(set(selected) - allowed)
        if invalid:
            raise ValueError(f"selected_analysts contains unsupported values: {invalid}")
        if not selected:
            raise ValueError("selected_analysts must include at least one analyst")
        self.selected_analysts = selected
        for key in ("max_debate_rounds", "max_risk_discuss_rounds"):
            value = getattr(self, key)
            if value is not None and value < 1:
                raise ValueError(f"{key} must be >= 1")

    def run_config(self) -> dict[str, Any]:
        config = deepcopy(DEFAULT_CONFIG)
        if self.output_language:
            config["output_language"] = self.output_language
        if self.max_debate_rounds is not None:
            config["max_debate_rounds"] = self.max_debate_rounds
        if self.max_risk_discuss_rounds is not None:
            config["max_risk_discuss_rounds"] = self.max_risk_discuss_rounds
        return config


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


class JobStore:
    def __init__(self, root: Path | None = None):
        self.root = root or default_state_dir()
        self.jobs_dir = self.root / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create(self, request: AnalysisRequest) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "scope": "user_behavior",
            "status": "queued",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "request": asdict(request),
            "events": [
                {
                    "time": utc_now(),
                    "type": "queued",
                    "message": "分析请求已接收。",
                }
            ],
            "system_snapshot": system_settings_snapshot(request.run_config()),
            "result": None,
            "error": None,
        }
        self.save(job)
        return job

    def save(self, job: dict[str, Any]) -> None:
        job["updated_at"] = utc_now()
        with self._lock:
            self._save_unlocked(job)

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            job = self._read_unlocked(job_id)
            job.update(changes)
            self._save_unlocked(job)
            return deepcopy(job)

    def add_event(self, job_id: str, event_type: str, message: str) -> None:
        with self._lock:
            job = self._read_unlocked(job_id)
            job.setdefault("events", []).append(
                {"time": utc_now(), "type": event_type, "message": message}
            )
            self._save_unlocked(job)

    def heartbeat(self, job_id: str) -> None:
        with self._lock:
            job = self._read_unlocked(job_id)
            if job.get("status") != "running":
                return
            job["heartbeat_at"] = utc_now()
            self._save_unlocked(job)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked(job_id)

    def get_view(self, job_id: str) -> dict[str, Any]:
        return self._decorate_runtime(self.get(job_id))

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        jobs = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in self.jobs_dir.glob("*.json")
        ]
        jobs.sort(key=_job_created_sort_key, reverse=True)
        return jobs[:limit]

    def list_view(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self._decorate_runtime(job) for job in self.list(limit)]

    def list_reports(self, limit: int = 100) -> list[dict[str, Any]]:
        reports: dict[str, dict[str, Any]] = {}
        for job in self.list(limit=1000):
            result = job.get("result") or {}
            report_path = result.get("report_path")
            if not report_path:
                continue
            record = _report_record_from_path(
                Path(report_path),
                job_id=job.get("id"),
                ticker=(job.get("request") or {}).get("ticker"),
                trade_date=(job.get("request") or {}).get("trade_date"),
                created_at=job.get("completed_at") or job.get("updated_at"),
            )
            if record:
                reports[record["id"]] = record

        reports_root = Path(DEFAULT_CONFIG["results_dir"]) / "reports"
        if reports_root.exists():
            for path in reports_root.glob("*/complete_report.md"):
                record = _report_record_from_path(path)
                if record and record["id"] not in reports:
                    reports[record["id"]] = record

        ordered = sorted(reports.values(), key=_report_created_sort_key, reverse=True)
        return ordered[:limit]

    def get_report(self, report_id: str) -> dict[str, Any]:
        for report in self.list_reports(limit=1000):
            if report["id"] == report_id:
                content = Path(report["path"]).read_text(encoding="utf-8", errors="replace")
                return {**report, "content": content}
        raise KeyError(report_id)

    def report_path(self, report_id: str) -> Path:
        report = self.get_report(report_id)
        return Path(report["path"])

    def recover_interrupted_jobs(self) -> int:
        recovered = 0
        with self._lock:
            for path in self.jobs_dir.glob("*.json"):
                job = json.loads(path.read_text(encoding="utf-8"))
                if job.get("status") not in {"queued", "running"}:
                    continue
                job["status"] = "failed"
                job["completed_at"] = utc_now()
                job["error"] = {
                    "message": "服务重启或后台线程退出，任务未完成；请重新发起分析。",
                }
                job.setdefault("events", []).append(
                    {
                        "time": utc_now(),
                        "type": "failed",
                        "message": "检测到未完成的历史任务，已标记为中断。",
                    }
                )
                self._save_unlocked(job)
                recovered += 1
        return recovered

    def path(self, job_id: str) -> Path:
        safe_id = "".join(ch for ch in job_id if ch.isalnum() or ch in "-_")
        return self.jobs_dir / f"{safe_id}.json"

    def _read_unlocked(self, job_id: str) -> dict[str, Any]:
        path = self.path(job_id)
        if not path.exists():
            raise KeyError(job_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_unlocked(self, job: dict[str, Any]) -> None:
        job["updated_at"] = utc_now()
        path = self.path(job["id"])
        tmp_path = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        tmp_path.write_text(
            json.dumps(job, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp_path.replace(path)

    def _decorate_runtime(self, job: dict[str, Any]) -> dict[str, Any]:
        view = deepcopy(job)
        runtime = _runtime_status(view)
        view["runtime"] = runtime
        if runtime["is_stale"]:
            view["health"] = "stale"
            view["health_message"] = (
                f"超过 {runtime['stale_after_seconds']} 秒没有心跳，可能卡在外部网络或模型接口。"
            )
        elif view.get("status") == "running":
            view["health"] = "active"
            view["health_message"] = "后台任务仍在运行。"
        else:
            view["health"] = "settled"
            view["health_message"] = ""
        return view


def run_analysis_job(job_id: str, store: JobStore) -> None:
    job = store.get(job_id)
    request = AnalysisRequest.from_mapping(job["request"])
    stop_heartbeat = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        store.update(job_id, status="running", started_at=utc_now(), heartbeat_at=utc_now())
        heartbeat_thread = _start_heartbeat(job_id, store, stop_heartbeat)
        store.add_event(job_id, "system", "已加载部署者维护的系统设置。")
        store.add_event(job_id, "user_behavior", "开始执行用户发起的分析任务。")
        config = request.run_config()
        store.add_event(job_id, "system", "正在初始化分析图与模型客户端。")
        graph = TradingAgentsGraph(
            selected_analysts=tuple(request.selected_analysts),
            debug=False,
            config=config,
        )
        store.add_event(job_id, "user_behavior", "正在请求数据源与模型接口生成分析结果。")
        final_state, decision = graph.propagate(
            request.ticker,
            request.trade_date,
            asset_type=request.asset_type or "stock",
        )
        store.add_event(job_id, "system", "正在保存分析报告。")
        report_path = graph.save_reports(final_state, request.ticker)
        store.update(
            job_id,
            status="completed",
            completed_at=utc_now(),
            result={
                "decision": decision,
                "final_trade_decision": final_state.get("final_trade_decision"),
                "report_path": str(report_path),
                "reports": _extract_report_sections(final_state),
            },
        )
        store.add_event(job_id, "completed", "分析已完成，报告已保存。")
    except Exception as exc:
        store.update(
            job_id,
            status="failed",
            error={"message": str(exc), "traceback": traceback.format_exc()},
            completed_at=utc_now(),
        )
        store.add_event(job_id, "failed", str(exc))
    finally:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1)


def _extract_report_sections(final_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_report": final_state.get("market_report"),
        "sentiment_report": final_state.get("sentiment_report"),
        "news_report": final_state.get("news_report"),
        "fundamentals_report": final_state.get("fundamentals_report"),
        "research_manager": (final_state.get("investment_debate_state") or {}).get("judge_decision"),
        "trader": final_state.get("trader_investment_plan"),
        "portfolio_manager": (final_state.get("risk_debate_state") or {}).get("judge_decision"),
    }


def _start_heartbeat(job_id: str, store: JobStore, stop_event: threading.Event) -> threading.Thread:
    interval = _env_int("TRADINGAGENTS_WEB_HEARTBEAT_SECONDS", 15, minimum=1)

    def beat() -> None:
        while not stop_event.wait(interval):
            try:
                store.heartbeat(job_id)
            except Exception:
                return

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    return thread


def _runtime_status(job: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    started = _parse_time(job.get("started_at"))
    completed = _parse_time(job.get("completed_at"))
    updated = _parse_time(job.get("heartbeat_at") or job.get("updated_at"))
    stale_after = _env_int("TRADINGAGENTS_WEB_STALE_AFTER_SECONDS", 180, minimum=1)
    end = now if job.get("status") in {"queued", "running"} else completed or updated or now
    running_for = int((end - started).total_seconds()) if started else None
    idle_for = int((now - updated).total_seconds()) if job.get("status") == "running" and updated else None
    return {
        "running_for_seconds": running_for,
        "idle_for_seconds": idle_for,
        "stale_after_seconds": stale_after,
        "is_stale": job.get("status") == "running" and idle_for is not None and idle_for > stale_after,
    }


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _job_created_sort_key(job: dict[str, Any]) -> datetime:
    return _parse_time(job.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)


def _report_created_sort_key(report: dict[str, Any]) -> datetime:
    return _parse_time(report.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)


def _report_record_from_path(
    path: Path,
    *,
    job_id: str | None = None,
    ticker: str | None = None,
    trade_date: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any] | None:
    resolved = path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_file():
        return None
    if resolved.suffix.lower() != ".md":
        return None

    results_root = Path(DEFAULT_CONFIG["results_dir"]).expanduser().resolve()
    try:
        resolved.relative_to(results_root)
    except ValueError:
        return None

    stat = resolved.stat()
    parent_name = resolved.parent.name
    inferred_ticker = ticker
    if not inferred_ticker and "_" in parent_name:
        inferred_ticker = parent_name.rsplit("_", 1)[0]

    return {
        "id": hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:24],
        "scope": "report_artifact",
        "job_id": job_id,
        "ticker": inferred_ticker or "未知标的",
        "trade_date": trade_date,
        "title": f"{inferred_ticker or parent_name} 报告",
        "path": str(resolved),
        "file_name": resolved.name,
        "directory": str(resolved.parent),
        "created_at": created_at or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "size_bytes": stat.st_size,
        "download_url": f"/api/reports/{hashlib.sha256(str(resolved).encode('utf-8')).hexdigest()[:24]}/download",
    }


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    value = int(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value
