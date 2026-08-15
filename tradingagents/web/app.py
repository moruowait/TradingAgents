from __future__ import annotations

import os
import threading
from importlib.resources import files

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from tradingagents.web.service import (
    AnalysisRequest,
    JobStore,
    run_analysis_job,
    system_settings_snapshot,
)


def create_app(store: JobStore | None = None) -> FastAPI:
    app = FastAPI(title="TradingAgents Web", version="0.1.0")
    app.state.store = store or JobStore()
    app.state.store.recover_interrupted_jobs()
    static_dir = files("tradingagents.web").joinpath("static")

    app.mount(
        "/static",
        StaticFiles(directory=str(static_dir)),
        name="static",
    )

    @app.get("/")
    def index():
        return FileResponse(str(static_dir / "index.html"))

    @app.get("/api/system-settings")
    def get_system_settings(_: None = Depends(require_admin)):
        return system_settings_snapshot()

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": app.state.store.list_view()}

    @app.post("/api/jobs")
    def create_job(payload: dict):
        try:
            request = AnalysisRequest.from_mapping(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        job = app.state.store.create(request)
        thread = threading.Thread(
            target=run_analysis_job,
            args=(job["id"], app.state.store),
            daemon=True,
        )
        thread.start()
        return job

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        try:
            return app.state.store.get_view(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/api/reports")
    def list_reports():
        return {"reports": app.state.store.list_reports()}

    @app.get("/api/reports/{report_id}")
    def get_report(report_id: str):
        try:
            return app.state.store.get_report(report_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="report not found") from exc

    @app.get("/api/reports/{report_id}/download")
    def download_report(report_id: str):
        try:
            path = app.state.store.report_path(report_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="report not found") from exc
        return FileResponse(
            str(path),
            media_type="text/markdown; charset=utf-8",
            filename=path.name,
        )

    return app


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    token = os.environ.get("TRADINGAGENTS_WEB_ADMIN_TOKEN")
    if token and x_admin_token != token:
        raise HTTPException(status_code=403, detail="admin token required")


def run() -> None:
    host = os.environ.get("TRADINGAGENTS_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("TRADINGAGENTS_WEB_PORT", "8000"))
    uvicorn.run("tradingagents.web.app:create_app", host=host, port=port, factory=True)


if __name__ == "__main__":
    run()
