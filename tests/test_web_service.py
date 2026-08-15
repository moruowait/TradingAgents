from __future__ import annotations

import json
import builtins
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.web.app import create_app
from tradingagents.web.service import (
    AnalysisRequest,
    JobStore,
    run_analysis_job,
    system_settings_snapshot,
)


def test_analysis_request_separates_user_behavior_from_system_config():
    request = AnalysisRequest.from_mapping(
        {
            "ticker": " spy ",
            "trade_date": "2026-01-15",
            "selected_analysts": ["market", "news"],
            "output_language": "中文",
            "max_debate_rounds": 2,
            "max_risk_discuss_rounds": 3,
        }
    )

    config = request.run_config()

    assert request.ticker == "SPY"
    assert request.asset_type == "stock"
    assert request.selected_analysts == ["market", "news"]
    assert config["output_language"] == "中文"
    assert config["max_debate_rounds"] == 2
    assert config["max_risk_discuss_rounds"] == 3
    assert "llm_provider" in config


def test_crypto_request_rejects_fundamentals_analyst():
    with pytest.raises(ValueError, match="unsupported"):
        AnalysisRequest.from_mapping(
            {
                "ticker": "BTC-USD",
                "trade_date": "2026-01-15",
                "selected_analysts": ["market", "fundamentals"],
            }
        )


def test_system_settings_snapshot_redacts_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "compatible-test")
    snapshot = system_settings_snapshot()

    secret = next(item for item in snapshot["secrets"] if item["key"] == "OPENAI_API_KEY")
    compatible_secret = next(
        item for item in snapshot["secrets"] if item["key"] == "OPENAI_COMPATIBLE_API_KEY"
    )

    assert secret["configured"] is True
    assert secret["value"] == "***configured***"
    assert compatible_secret["configured"] is True
    assert compatible_secret["value"] == "***configured***"
    assert snapshot["deployment"]["scope"] == "system"
    assert snapshot["user_behavior_schema"]["scope"] == "user_behavior"


def test_job_store_persists_user_behavior_scope(tmp_path):
    store = JobStore(tmp_path)
    request = AnalysisRequest.from_mapping({"ticker": "AAPL", "trade_date": "2026-01-15"})

    job = store.create(request)
    path = store.path(job["id"])

    assert job["scope"] == "user_behavior"
    assert path.exists()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["request"]["ticker"] == "AAPL"
    assert persisted["system_snapshot"]["deployment"]["scope"] == "system"


def test_job_view_marks_stale_running_task(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_WEB_STALE_AFTER_SECONDS", "30")
    store = JobStore(tmp_path)
    request = AnalysisRequest.from_mapping(
        {
            "ticker": "BTC-USD",
            "trade_date": "2026-01-15",
            "selected_analysts": ["market", "social", "news"],
        }
    )

    job = store.create(request)
    old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    store.update(job["id"], status="running", started_at=old, heartbeat_at=old, updated_at=old)

    view = store.get_view(job["id"])

    assert view["health"] == "stale"
    assert view["runtime"]["is_stale"] is True
    assert "可能卡在外部网络" in view["health_message"]


def test_recover_interrupted_jobs_marks_orphans_failed(tmp_path):
    store = JobStore(tmp_path)
    request = AnalysisRequest.from_mapping({"ticker": "AAPL", "trade_date": "2026-01-15"})
    job = store.create(request)
    store.update(job["id"], status="running", started_at=datetime.now(timezone.utc).isoformat())

    assert store.recover_interrupted_jobs() == 1

    recovered = store.get(job["id"])
    assert recovered["status"] == "failed"
    assert "请重新发起分析" in recovered["error"]["message"]


def test_completed_job_runtime_uses_completed_time(tmp_path):
    store = JobStore(tmp_path)
    request = AnalysisRequest.from_mapping({"ticker": "AAPL", "trade_date": "2026-01-15"})
    job = store.create(request)
    started = datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc).isoformat()
    completed = datetime(2026, 1, 15, 1, 2, 30, tzinfo=timezone.utc).isoformat()
    store.update(job["id"], status="completed", started_at=started, completed_at=completed)

    view = store.get_view(job["id"])

    assert view["runtime"]["running_for_seconds"] == 150
    assert view["runtime"]["idle_for_seconds"] is None


def test_job_list_sorts_by_created_time_not_file_update_time(tmp_path):
    store = JobStore(tmp_path)
    older = store.create(AnalysisRequest.from_mapping({"ticker": "AAPL", "trade_date": "2026-01-15"}))
    newer = store.create(AnalysisRequest.from_mapping({"ticker": "MSFT", "trade_date": "2026-01-16"}))
    store.update(older["id"], status="failed", error={"message": "updated later"})

    jobs = store.list()

    assert [job["id"] for job in jobs[:2]] == [newer["id"], older["id"]]


def test_report_history_exposes_preview_content(tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(results_dir))
    store = JobStore(tmp_path / "state")
    report_path = results_dir / "reports" / "BTC-USD_20260115_120000" / "complete_report.md"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("# BTC-USD Report\n\n**Rating**: Hold", encoding="utf-8")
    job = store.create(
        AnalysisRequest.from_mapping(
            {
                "ticker": "BTC-USD",
                "trade_date": "2026-01-15",
                "selected_analysts": ["market", "social", "news"],
            }
        )
    )
    store.update(
        job["id"],
        status="completed",
        completed_at="2026-01-15T12:00:00+00:00",
        result={"report_path": str(report_path)},
    )

    reports = store.list_reports()
    report = store.get_report(reports[0]["id"])

    assert reports[0]["scope"] == "report_artifact"
    assert reports[0]["ticker"] == "BTC-USD"
    assert reports[0]["job_id"] == job["id"]
    assert report["content"].startswith("# BTC-USD Report")
    assert report["download_url"].endswith("/download")


def test_report_history_ignores_paths_outside_results_dir(tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(results_dir))
    store = JobStore(tmp_path / "state")
    outside_report = tmp_path / "outside" / "complete_report.md"
    outside_report.parent.mkdir()
    outside_report.write_text("# Outside", encoding="utf-8")
    job = store.create(AnalysisRequest.from_mapping({"ticker": "AAPL", "trade_date": "2026-01-15"}))
    store.update(
        job["id"],
        status="completed",
        result={"report_path": str(outside_report)},
    )

    assert store.list_reports() == []


def test_report_download_endpoint_returns_markdown(tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(results_dir))
    store = JobStore(tmp_path / "state")
    report_path = results_dir / "reports" / "AAPL_20260115_120000" / "complete_report.md"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("# AAPL Report", encoding="utf-8")
    job = store.create(AnalysisRequest.from_mapping({"ticker": "AAPL", "trade_date": "2026-01-15"}))
    store.update(
        job["id"],
        status="completed",
        completed_at="2026-01-15T12:00:00+00:00",
        result={"report_path": str(report_path)},
    )
    report = store.list_reports()[0]
    client = TestClient(create_app(store))

    preview = client.get(f"/api/reports/{report['id']}")
    download = client.get(f"/api/reports/{report['id']}/download")

    assert preview.status_code == 200
    assert preview.json()["content"] == "# AAPL Report"
    assert download.status_code == 200
    assert download.text == "# AAPL Report"


def test_run_analysis_job_marks_import_failure_as_failed(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    request = AnalysisRequest.from_mapping(
        {
            "ticker": "BTC-USD",
            "trade_date": "2026-01-15",
            "selected_analysts": ["market", "social", "news"],
        }
    )
    job = store.create(request)
    original_import = builtins.__import__

    def fail_graph_import(name, *args, **kwargs):
        if name == "tradingagents.graph.trading_graph":
            raise ModuleNotFoundError("missing graph dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_graph_import)

    run_analysis_job(job["id"], store)

    stored = store.get(job["id"])
    assert stored["status"] == "failed"
    assert "missing graph dependency" in stored["error"]["message"]
