"""FastAPI entrypoint. Mounts /health, /version, and /v1/*.

Supabase owns all persistent state (users, subscriptions, device_codes,
usage_ledger) — this backend is stateless. State-shaped operations reach
Supabase via its REST API (see auth.py).
"""
import os
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .routers import v1
from .settings import get_settings

VERSION = "0.1.0"
_settings = get_settings()

# agent.py reads GEMINI_API_KEY from os.environ. Bridge our
# TRACER_GEMINI_API_KEY into that name at startup so we don't have to
# fork agent.py's key-loading logic.
if _settings.gemini_api_key and not os.environ.get("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = _settings.gemini_api_key


app = FastAPI(title="Tracer Backend", version=VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def _access_log(request: Request, call_next):
    # One line per request. Render/Fly aggregate stdout automatically,
    # so no logging config needed. user_id is not on the request here
    # (JWT verify is per-route), so the plugin version + IP + path is
    # what a bug report can be matched against.
    start = time.monotonic()
    resp = await call_next(request)
    dur_ms = int((time.monotonic() - start) * 1000)
    print(
        f"[req] {request.method} {request.url.path} -> {resp.status_code} "
        f"{dur_ms}ms plugin={request.headers.get('x-tracer-version', '-')} "
        f"ip={request.client.host if request.client else '-'}",
        flush=True,
    )
    return resp


app.include_router(v1.router)


@app.get("/health")
async def health() -> dict:
    """Liveness — surfaces missing config (Supabase URL, JWT secret, Gemini key)."""
    return {
        "ok": True,
        "version": VERSION,
        "supabase_configured": bool(_settings.supabase_url and _settings.supabase_service_role_key),
        "jwt_secret_configured": _settings.device_jwt_secret != "dev-only-change-me",
        "gemini_configured": bool(_settings.gemini_api_key),
    }


@app.get("/version")
async def version() -> dict:
    return {"version": VERSION}
