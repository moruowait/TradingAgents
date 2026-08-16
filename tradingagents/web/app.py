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
    DEFAULT_USER_ID,
    JobStore,
    configured_admin_user_id,
    normalize_user_id,
    run_analysis_job,
    system_settings_snapshot,
)


def create_app(store: JobStore | None = None) -> FastAPI:
    app = FastAPI(title="TradingAgents Web", version="0.1.0")
    app.state.store = store or JobStore()
    app.state.store.recover_interrupted_jobs()
    static_dir = files("tradingagents.web").joinpath("static")

    def resolve_current_user(token: str = Depends(current_session_token)) -> dict:
        user = app.state.store.user_for_session(token)
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录")
        return user

    app.dependency_overrides[current_user] = resolve_current_user

    app.mount(
        "/static",
        StaticFiles(directory=str(static_dir)),
        name="static",
    )

    @app.get("/")
    def index():
        return FileResponse(str(static_dir / "index.html"))

    @app.get("/admin")
    def admin_index():
        return FileResponse(str(static_dir / "index.html"))

    @app.post("/api/auth/login")
    def login(payload: dict):
        user_id = normalize_user_id(str(payload.get("user_id") or ""))
        password = str(payload.get("password") or "")
        user = app.state.store.authenticate_user(user_id, password)
        if user is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        return app.state.store.create_session(user["user_id"])

    @app.get("/api/auth/me")
    def me(user: dict = Depends(current_user)):
        return {"user": user}

    @app.post("/api/auth/logout")
    def logout(token: str = Depends(current_session_token)):
        app.state.store.delete_session(token)
        return {"ok": True}

    @app.get("/api/system-settings")
    def get_system_settings(_: dict = Depends(require_admin)):
        return system_settings_snapshot()

    @app.get("/api/users")
    def list_users():
        return {"users": app.state.store.list_users(), "default_user_id": DEFAULT_USER_ID}

    @app.get("/api/admin/users")
    def admin_list_users(_: dict = Depends(require_admin)):
        return {
            "users": app.state.store.list_users(),
            "default_user_id": DEFAULT_USER_ID,
            "admin_user_id": configured_admin_user_id(),
        }

    @app.post("/api/admin/users")
    def admin_create_user(payload: dict, _: dict = Depends(require_admin)):
        try:
            user_id = normalize_user_id(str(payload.get("user_id") or ""))
            if user_id == DEFAULT_USER_ID or user_id == configured_admin_user_id():
                raise ValueError("user already exists or is protected")
            display_name = str(payload.get("display_name") or user_id).strip() or user_id
            password = str(payload.get("password") or "")
            return app.state.store.create_user(user_id, display_name=display_name, password=password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/admin/users/{user_id}")
    def admin_delete_user(user_id: str, _: dict = Depends(require_admin)):
        try:
            return app.state.store.delete_user(user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="user not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/jobs")
    def list_jobs(user: dict = Depends(current_user)):
        user_id = user["user_id"]
        return {"jobs": app.state.store.list_view(user_id=user_id)}

    @app.post("/api/jobs")
    def create_job(payload: dict, user: dict = Depends(current_user)):
        user_id = user["user_id"]
        try:
            request = AnalysisRequest.from_mapping(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        job = app.state.store.create(request, user_id=user_id)
        thread = threading.Thread(
            target=run_analysis_job,
            args=(job["id"], app.state.store),
            daemon=True,
        )
        thread.start()
        return job

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, user: dict = Depends(current_user)):
        user_id = user["user_id"]
        try:
            return app.state.store.get_view(job_id, user_id=user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/api/reports")
    def list_reports(user: dict = Depends(current_user)):
        user_id = user["user_id"]
        return {"reports": app.state.store.list_reports(user_id=user_id)}

    @app.get("/api/reports/{report_id}")
    def get_report(report_id: str, user: dict = Depends(current_user)):
        user_id = user["user_id"]
        try:
            return app.state.store.get_report(report_id, user_id=user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="report not found") from exc

    @app.get("/api/reports/{report_id}/download")
    def download_report(report_id: str, user: dict = Depends(current_user)):
        user_id = user["user_id"]
        try:
            path = app.state.store.report_path(report_id, user_id=user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="report not found") from exc
        return FileResponse(
            str(path),
            media_type="text/markdown; charset=utf-8",
            filename=path.name,
        )

    return app


def current_session_token(
    auth_token: str | None = None,
    x_auth_token: str | None = Header(default=None),
) -> str:
    return x_auth_token or auth_token or ""


def current_user(token: str = Depends(current_session_token)) -> dict:
    # FastAPI cannot inject app.state into this standalone dependency, so the
    # route wrappers bind it by overriding this dependency at app creation.
    raise HTTPException(status_code=401, detail="请先登录")


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员账号")
    return user


def run() -> None:
    host = os.environ.get("TRADINGAGENTS_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("TRADINGAGENTS_WEB_PORT", "8000"))
    uvicorn.run("tradingagents.web.app:create_app", host=host, port=port, factory=True)


if __name__ == "__main__":
    run()
