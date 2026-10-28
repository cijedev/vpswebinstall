"""Shared-secret authentication (cookie, bearer, or X-API-Token)."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from cije_vpsinstallerweb.settings import Settings


def _extract_token(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    api = request.headers.get("x-api-token")
    if api:
        return api.strip()
    cookie = request.cookies.get(request.app.state.settings.cookie_name)
    if cookie:
        return cookie.strip()
    query = request.query_params.get("token")
    if query:
        return query.strip()
    return ""


def tokens_match(provided: str, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def require_token(request: Request) -> str:
    settings: Settings = request.app.state.settings
    provided = _extract_token(request)
    if tokens_match(provided, settings.token):
        return provided
    accept = request.headers.get("accept", "")
    if "text/html" in accept and request.url.path != "/login":
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login?next=" + str(request.url.path)},
        )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing token")


AuthDep = Annotated[str, Depends(require_token)]


def login_redirect(next_path: str = "/") -> RedirectResponse:
    target = next_path if next_path.startswith("/") else "/"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
