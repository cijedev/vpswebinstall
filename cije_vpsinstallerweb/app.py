"""FastAPI application: pages + JSON API wrapping debian_vps_setup."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from cije_vpsinstallerweb import __version__
from cije_vpsinstallerweb.auth import tokens_match
from cije_vpsinstallerweb.bridge import (
    choices,
    dns_payload,
    list_backup_payload,
    load_or_default,
    make_runner,
    save_config_file,
    status_payload,
    validate_payload,
)
from cije_vpsinstallerweb.jobs import JobManager
from cije_vpsinstallerweb.paths import STATIC, TEMPLATES
from cije_vpsinstallerweb.settings import Settings, load_settings

PUBLIC_PATHS = {"/login", "/health", "/api/health"}
STATIC_PREFIX = "/static"


class TokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(STATIC_PREFIX + "/"):
            return await call_next(request)
        settings: Settings = request.app.state.settings
        provided = _token_from_request(request)
        if tokens_match(provided, settings.token):
            return await call_next(request)
        accept = request.headers.get("accept", "")
        if path.startswith("/api/") or "application/json" in accept:
            return JSONResponse({"detail": "invalid or missing token"}, status_code=401)
        nxt = quote(path)
        return RedirectResponse(f"/login?next={nxt}", status_code=303)


def _token_from_request(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    api = request.headers.get("x-api-token")
    if api:
        return api.strip()
    settings: Settings = request.app.state.settings
    cookie = request.cookies.get(settings.cookie_name)
    if cookie:
        return cookie.strip()
    query = request.query_params.get("token")
    if query:
        return query.strip()
    return ""


def _context(request: Request, **extra: Any) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    jobs: JobManager = request.app.state.jobs
    current = jobs.current()
    latest = jobs.latest()
    ctx = {
        "request": request,
        "app_name": "cije-vpsinstallerweb",
        "app_title": "Cije VPS Installer",
        "version": __version__,
        "choices": choices(),
        "settings": settings,
        "live_mode": settings.live_mode,
        "dry_run": not settings.live_mode,
        "install_root": str(settings.install_root),
        "current_job": current.snapshot() if current else None,
        "latest_job": latest.snapshot() if latest else None,
    }
    ctx.update(extra)
    return ctx


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(
        title="cije-vpsinstallerweb",
        version=__version__,
        description="Web UI for the Cije VPS Installer. Does not replace the CLI.",
    )
    app.state.settings = settings
    app.state.jobs = JobManager()
    templates = Jinja2Templates(directory=str(TEMPLATES))
    app.state.templates = templates

    app.add_middleware(TokenMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    def render(request: Request, name: str, status_code: int = 200, **extra: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request, name, _context(request, **extra), status_code=status_code
        )

    @app.get("/health")
    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "app": "cije-vpsinstallerweb",
            "version": __version__,
            "cli_version": choices()["cli_version"],
            "dry_run": not settings.live_mode,
            "live_mode": settings.live_mode,
        }

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/"):
        if tokens_match(_token_from_request(request), settings.token):
            return RedirectResponse(next if next.startswith("/") else "/", status_code=303)
        return render(request, "login.html", next_path=next, error=None)

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        token = str(form.get("token") or "").strip()
        next_path = str(form.get("next") or "/")
        if not next_path.startswith("/"):
            next_path = "/"
        if not tokens_match(token, settings.token):
            return render(
                request,
                "login.html",
                next_path=next_path,
                error="That token does not match CIJE_WEB_TOKEN.",
                status_code=401,
            )
        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(
            settings.cookie_name,
            token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=60 * 60 * 24 * 14,
        )
        return response

    @app.post("/logout")
    async def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(settings.cookie_name)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        cfg = load_or_default(settings)
        return render(
            request,
            "dashboard.html",
            config=cfg.to_dict(),
            public=cfg.public_dict(),
            status=status_payload(settings),
        )

    @app.get("/wizard", response_class=HTMLResponse)
    async def wizard(request: Request):
        cfg = load_or_default(settings)
        return render(request, "wizard.html", config=cfg.to_dict())

    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request):
        cfg = load_or_default(settings)
        return render(
            request,
            "status.html",
            config=cfg.to_dict(),
            public=cfg.public_dict(),
            status=status_payload(settings),
        )

    @app.get("/dns", response_class=HTMLResponse)
    async def dns_page(request: Request):
        cfg = load_or_default(settings)
        runner = make_runner(settings)
        return render(request, "dns.html", config=cfg.to_dict(), dns=dns_payload(cfg, runner))

    @app.get("/troubleshoot", response_class=HTMLResponse)
    async def troubleshoot_page(request: Request):
        cfg = load_or_default(settings)
        return render(request, "troubleshoot.html", config=cfg.to_dict())

    @app.get("/backup", response_class=HTMLResponse)
    async def backup_page(request: Request):
        from debian_vps_setup.runner import Runner

        listing = list_backup_payload(Runner(dry_run=False, root=str(settings.install_root)))
        return render(request, "backup.html", backups=listing)

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request, job: str | None = None):
        jobs: JobManager = request.app.state.jobs
        target = jobs.get(job) if job else jobs.latest()
        return render(request, "logs.html", job=target.snapshot() if target else None)

    @app.get("/api/meta")
    async def api_meta():
        return {
            "app": "cije-vpsinstallerweb",
            "version": __version__,
            **choices(),
            "dry_run": not settings.live_mode,
            "live_mode": settings.live_mode,
            "install_root": str(settings.install_root),
            "host": settings.host,
            "port": settings.port,
        }

    @app.get("/api/config")
    async def api_get_config(redact: bool = False):
        cfg = load_or_default(settings)
        problems = cfg.validate()
        body = cfg.public_dict() if redact else cfg.to_dict()
        return {"ok": not problems, "problems": problems, "config": body}

    @app.put("/api/config")
    @app.post("/api/config")
    async def api_put_config(request: Request):
        data = await request.json()
        if not isinstance(data, dict):
            return JSONResponse({"ok": False, "problems": ["body must be a JSON object"]}, status_code=400)
        try:
            cfg, problems = validate_payload(data)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "problems": [str(exc)]}, status_code=400)
        if problems:
            return JSONResponse({"ok": False, "problems": problems, "config": cfg.to_dict()}, status_code=400)
        save_config_file(settings.config_path, cfg)
        return {"ok": True, "problems": [], "config": cfg.to_dict()}

    @app.get("/api/status")
    async def api_status():
        return status_payload(settings)

    @app.get("/api/dns")
    async def api_dns():
        cfg = load_or_default(settings)
        return dns_payload(cfg, make_runner(settings))

    @app.get("/api/report")
    async def api_report():
        payload = status_payload(settings)
        return {"ok": payload["has_report"], "path": payload["report_path"], "report": payload["report"]}

    @app.get("/api/backup")
    async def api_backup_list():
        from debian_vps_setup.runner import Runner

        return list_backup_payload(Runner(dry_run=False, root=str(settings.install_root)))

    @app.get("/api/jobs")
    async def api_jobs():
        jobs: JobManager = request_jobs(app)
        current = jobs.current()
        latest = jobs.latest()
        return {
            "busy": jobs.busy(),
            "current": current.snapshot() if current else None,
            "latest": latest.snapshot() if latest else None,
        }

    @app.get("/api/jobs/{job_id}")
    async def api_job(job_id: str):
        jobs: JobManager = request_jobs(app)
        job = jobs.get(job_id)
        if not job:
            return JSONResponse({"detail": "job not found"}, status_code=404)
        return job.snapshot()

    @app.get("/api/jobs/{job_id}/stream")
    async def api_job_stream(job_id: str, request: Request):
        jobs: JobManager = request_jobs(app)
        job = jobs.get(job_id)
        if not job:
            return JSONResponse({"detail": "job not found"}, status_code=404)

        async def events():
            idx = 0
            while True:
                if await request.is_disconnected():
                    break
                with job._cv:
                    batch = job.lines[idx:]
                    idx = len(job.lines)
                    done = job.status not in {"queued", "running"}
                    status = job.status
                    result = job.result if done else None
                for line in batch:
                    yield f"data: {json.dumps({'line': line})}\n\n"
                if done:
                    yield f"data: {json.dumps({'done': True, 'status': status, 'result': result})}\n\n"
                    break
                await asyncio.sleep(0.15)

        return StreamingResponse(events(), media_type="text/event-stream")

    def _start_or_409(start_fn):
        jobs: JobManager = request_jobs(app)
        try:
            job = start_fn()
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=409)
        return {"ok": True, "job": job.snapshot()}

    @app.post("/api/install")
    async def api_install(request: Request):
        body = await _json_body(request)
        cfg, problems = _cfg_from_body(body)
        if problems:
            return JSONResponse({"ok": False, "problems": problems}, status_code=400)
        only = _as_list(body.get("only"))
        skip = _as_list(body.get("skip"))
        return _start_or_409(
            lambda: app.state.jobs.submit_install(
                settings,
                {"force": bool(body.get("force")), "only": only, "skip": skip, "no_backup": bool(body.get("no_backup"))},
                cfg,
            )
        )

    @app.post("/api/test")
    async def api_test(request: Request):
        body = await _json_body(request)
        cfg, problems = _cfg_from_body(body)
        if problems:
            return JSONResponse({"ok": False, "problems": problems}, status_code=400)
        return _start_or_409(lambda: app.state.jobs.submit_test(settings, cfg))

    @app.post("/api/troubleshoot")
    async def api_troubleshoot(request: Request):
        body = await _json_body(request)
        cfg, problems = _cfg_from_body(body)
        if problems:
            return JSONResponse({"ok": False, "problems": problems}, status_code=400)
        components = _as_list(body.get("components") or body.get("component"))
        try:
            return _start_or_409(lambda: app.state.jobs.submit_troubleshoot(settings, cfg, components))
        except ValueError as exc:
            return JSONResponse({"ok": False, "problems": [str(exc)]}, status_code=400)

    @app.post("/api/backup")
    async def api_backup_create():
        return _start_or_409(lambda: app.state.jobs.submit_backup(settings))

    @app.post("/api/backup/{name}/restore")
    async def api_backup_restore(name: str):
        return _start_or_409(lambda: app.state.jobs.submit_restore(settings, name))

    def _cfg_from_body(body: dict[str, Any]):
        if isinstance(body.get("config"), dict):
            return validate_payload(body["config"])
        if any(key in body for key in ("domain", "hostname", "mailbaby", "webmail", "control_panel")):
            return validate_payload(body)
        cfg = load_or_default(settings)
        return cfg, cfg.validate()

    return app


def request_jobs(app: FastAPI) -> JobManager:
    return app.state.jobs


async def _json_body(request: Request) -> dict[str, Any]:
    if not request.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _as_list(value: Any) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return parts or None
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return None
