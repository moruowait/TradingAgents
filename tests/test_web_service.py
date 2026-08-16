from __future__ import annotations

import json
import builtins
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.web.app import create_app
from tradingagents.web.service import (
    AnalysisRequest,
    DEFAULT_USER_ID,
    JobStore,
    configured_admin_user_id,
    normalize_user_id,
    run_analysis_job,
    system_settings_snapshot,
)


def auth_headers(client: TestClient, user_id: str = DEFAULT_USER_ID, password: str = "123456"):
    response = client.post(
        "/api/auth/login",
        json={"user_id": user_id, "password": password},
    )
    assert response.status_code == 200
    return {"X-Auth-Token": response.json()["token"]}


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
    data_secret = next(item for item in snapshot["secrets"] if item["key"] == "ALPHA_VANTAGE_API_KEY")
    llm_provider = next(item for item in snapshot["settings"] if item["key"] == "llm_provider")
    results_dir = next(item for item in snapshot["settings"] if item["key"] == "results_dir")

    assert secret["configured"] is True
    assert secret["value"] == "***configured***"
    assert secret["group"] == "llm_credentials"
    assert compatible_secret["configured"] is True
    assert compatible_secret["value"] == "***configured***"
    assert compatible_secret["group"] == "llm_credentials"
    assert data_secret["group"] == "data_credentials"
    assert llm_provider["group"] == "llm_base"
    assert results_dir["group"] == "business_runtime"
    assert snapshot["groups"]["llm_base"]["title"] == "LLM 基础配置"
    assert snapshot["groups"]["business_runtime"]["title"] == "业务运行配置"
    assert snapshot["deployment"]["scope"] == "system"
    assert snapshot["deployment"]["group"] == "deployment"
    assert snapshot["user_behavior_schema"]["scope"] == "user_behavior"
    assert snapshot["user_behavior_schema"]["group"] == "user_behavior"


def test_job_store_persists_user_behavior_scope(tmp_path):
    store = JobStore(tmp_path)
    request = AnalysisRequest.from_mapping({"ticker": "AAPL", "trade_date": "2026-01-15"})

    job = store.create(request)

    assert job["scope"] == "user_behavior"
    assert job["user_id"] == DEFAULT_USER_ID
    assert store.db_path.exists()
    persisted = store.get(job["id"], user_id=DEFAULT_USER_ID)
    assert persisted["request"]["ticker"] == "AAPL"
    assert persisted["system_snapshot"]["deployment"]["scope"] == "system"


def test_normalize_user_id_keeps_storage_key_safe():
    assert normalize_user_id(" Alice. Zhang ") == "alicezhang"
    assert normalize_user_id("") == DEFAULT_USER_ID


def test_job_store_isolates_jobs_by_user(tmp_path):
    store = JobStore(tmp_path)
    alice_job = store.create(
        AnalysisRequest.from_mapping({"ticker": "AAPL", "trade_date": "2026-01-15"}),
        user_id="alice",
    )
    bob_job = store.create(
        AnalysisRequest.from_mapping({"ticker": "MSFT", "trade_date": "2026-01-15"}),
        user_id="bob",
    )

    assert [job["id"] for job in store.list(user_id="alice")] == [alice_job["id"]]
    assert [job["id"] for job in store.list(user_id="bob")] == [bob_job["id"]]
    with pytest.raises(KeyError):
        store.get(alice_job["id"], user_id="bob")


def test_job_store_initializes_default_admin_user(tmp_path):
    store = JobStore(tmp_path)
    users = {user["user_id"]: user for user in store.list_users()}

    assert configured_admin_user_id() == DEFAULT_USER_ID
    assert users[DEFAULT_USER_ID]["role"] == "admin"
    assert store.authenticate_user(DEFAULT_USER_ID, "123456")["role"] == "admin"


def test_job_store_migrates_legacy_default_admin_to_admin_user(tmp_path):
    db_path = tmp_path / "state.sqlite3"
    legacy_job = {
        "id": "legacy-job",
        "user_id": "default",
        "scope": "user_behavior",
        "status": "completed",
        "created_at": "2026-01-15T00:00:00+00:00",
        "updated_at": "2026-01-15T00:00:00+00:00",
        "request": {"ticker": "AAPL", "trade_date": "2026-01-15"},
        "events": [],
        "system_snapshot": {},
        "result": None,
        "error": None,
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                password_hash TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
            ("default", "默认用户", "admin", None, "2026-01-15T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?)",
            ("admin", "旧管理员", "user", None, "2026-01-16T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
            (
                "legacy-job",
                "default",
                "completed",
                "2026-01-15T00:00:00+00:00",
                "2026-01-15T00:00:00+00:00",
                json.dumps(legacy_job),
            ),
        )

    store = JobStore(tmp_path)
    users = {user["user_id"]: user for user in store.list_users()}
    migrated_job = store.get("legacy-job", user_id="admin")

    assert "default" not in users
    assert users["admin"]["role"] == "admin"
    assert users["admin"]["job_count"] == 1
    assert migrated_job["user_id"] == "admin"


def test_admin_user_management_adds_and_deletes_users(tmp_path):
    store = JobStore(tmp_path)
    client = TestClient(create_app(store))
    headers = auth_headers(client)

    assert client.get("/api/admin/users").status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"user_id": DEFAULT_USER_ID, "password": "wrong"},
    ).status_code == 401

    created = client.post(
        "/api/admin/users",
        headers=headers,
        json={"user_id": "Alice.Zhang", "display_name": "Alice Zhang", "password": "alice-pass"},
    )

    assert created.status_code == 200
    assert created.json()["user_id"] == "alicezhang"
    assert store.user_exists("alicezhang") is True
    assert store.authenticate_user("alicezhang", "alice-pass")["user_id"] == "alicezhang"

    store.create(
        AnalysisRequest.from_mapping({"ticker": "AAPL", "trade_date": "2026-01-15"}),
        user_id="alicezhang",
    )
    deleted = client.delete("/api/admin/users/alicezhang", headers=headers)

    assert deleted.status_code == 200
    assert deleted.json()["deleted_job_count"] == 1
    assert store.user_exists("alicezhang") is False
    assert store.list(user_id="alicezhang") == []
    assert client.delete(f"/api/admin/users/{DEFAULT_USER_ID}", headers=headers).status_code == 400


def test_analysis_api_requires_login(tmp_path):
    store = JobStore(tmp_path)
    client = TestClient(create_app(store))

    response = client.post(
        "/api/jobs",
        json={"ticker": "AAPL", "trade_date": "2026-01-15"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "请先登录"


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
    headers = auth_headers(client)

    preview = client.get(f"/api/reports/{report['id']}", headers=headers)
    download = client.get(f"/api/reports/{report['id']}/download", headers=headers)

    assert preview.status_code == 200
    assert preview.json()["content"] == "# AAPL Report"
    assert download.status_code == 200
    assert download.text == "# AAPL Report"


def test_report_endpoints_are_isolated_by_user(tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(results_dir))
    store = JobStore(tmp_path / "state")
    alice_report = results_dir / "reports" / "AAPL_20260115_120000" / "complete_report.md"
    bob_report = results_dir / "reports" / "MSFT_20260115_120000" / "complete_report.md"
    alice_report.parent.mkdir(parents=True)
    bob_report.parent.mkdir(parents=True)
    alice_report.write_text("# AAPL Report", encoding="utf-8")
    bob_report.write_text("# MSFT Report", encoding="utf-8")
    store.create_user("alice", display_name="Alice", password="alice-pass")
    store.create_user("bob", display_name="Bob", password="bob-pass")
    alice_job = store.create(
        AnalysisRequest.from_mapping({"ticker": "AAPL", "trade_date": "2026-01-15"}),
        user_id="alice",
    )
    bob_job = store.create(
        AnalysisRequest.from_mapping({"ticker": "MSFT", "trade_date": "2026-01-15"}),
        user_id="bob",
    )
    store.update(
        alice_job["id"],
        status="completed",
        completed_at="2026-01-15T12:00:00+00:00",
        result={"report_path": str(alice_report)},
    )
    store.update(
        bob_job["id"],
        status="completed",
        completed_at="2026-01-15T12:00:00+00:00",
        result={"report_path": str(bob_report)},
    )
    client = TestClient(create_app(store))
    alice_headers = auth_headers(client, "alice", "alice-pass")
    bob_headers = auth_headers(client, "bob", "bob-pass")

    alice_reports = client.get("/api/reports", headers=alice_headers).json()["reports"]
    bob_reports = client.get("/api/reports", headers=bob_headers).json()["reports"]

    assert [report["ticker"] for report in alice_reports] == ["AAPL"]
    assert [report["ticker"] for report in bob_reports] == ["MSFT"]
    assert client.get(
        f"/api/reports/{bob_reports[0]['id']}",
        headers=alice_headers,
    ).status_code == 404
    assert client.get(
        f"/api/reports/{bob_reports[0]['id']}/download",
        headers=alice_headers,
    ).status_code == 404


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
