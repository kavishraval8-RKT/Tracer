"""Bearer JWT auth for plugin requests.

Every /v1/* route runs `require_user` as a dependency. That dependency:

  1. Extracts the Authorization: Bearer <jwt> header.
  2. Verifies the JWT signature with TRACER_DEVICE_JWT_SECRET (HS256).
  3. Checks iss/aud/exp per RFC 7519.
  4. Looks up profiles.active_device_jti in Supabase and refuses if the
     JWT's jti no longer matches — this is the "one plugin per user"
     enforcement (a newer login rotates the jti; the old plugin dies).

Failure modes surface as 401 with a body the plugin can react to:
  - {"error": "expired_token"}    → prompt re-login
  - {"error": "revoked"}          → prompt re-login (another device took over)
  - {"error": "invalid_token"}    → prompt re-login
"""
from __future__ import annotations

from typing import Annotated

import httpx
import jwt
from fastapi import Depends, HTTPException, Request

from .settings import get_settings


class CurrentUser:
    """Small value object attached to a request after auth passes."""
    __slots__ = ("id", "tier", "jti")

    def __init__(self, *, id: str, tier: str, jti: str) -> None:
        self.id = id
        self.tier = tier
        self.jti = jti


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"error": "missing_token"})
    return header.split(" ", 1)[1].strip()


async def _current_active_jti(user_id: str) -> tuple[bool, str | None]:
    """Return (supabase_configured, active_device_jti).

    - (False, None) → dev mode, no Supabase — skip freshness check.
    - (True, "<jti>") → live, plugin must match.
    - (True, None)   → live, no active plugin (signed out / revoked) — reject.
    """
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        # Dev fallback: no Supabase configured → skip jti freshness check.
        # Do NOT ship this to prod without a configured Supabase.
        return (False, None)

    url = f"{settings.supabase_url}/rest/v1/profiles"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            url,
            params={"id": f"eq.{user_id}", "select": "active_device_jti"},
            headers={
                "apikey": settings.supabase_service_role_key,
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
                "Accept": "application/json",
            },
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail={"error": "profile_lookup_failed"})
    rows = resp.json()
    if not rows:
        return (True, None)
    return (True, rows[0].get("active_device_jti"))


async def require_user(request: Request) -> CurrentUser:
    token = _bearer(request)
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.device_jwt_secret,
            algorithms=["HS256"],
            issuer=settings.device_jwt_issuer,
            audience=settings.device_jwt_audience,
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail={"error": "expired_token"})
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail={"error": "invalid_token"})

    sub = payload.get("sub")
    jti = payload.get("jti")
    tier = payload.get("tier") or "hobby"
    if not sub or not jti:
        raise HTTPException(status_code=401, detail={"error": "invalid_token"})

    # jti freshness — the "one plugin per user" check. A newer activation
    # rotates active_device_jti on the profile; signing out on the web
    # clears it. In both cases the plugin's JWT should stop working.
    configured, active = await _current_active_jti(sub)
    if configured and active != jti:
        raise HTTPException(status_code=401, detail={"error": "revoked"})

    return CurrentUser(id=sub, tier=tier, jti=jti)


AuthedUser = Annotated[CurrentUser, Depends(require_user)]
