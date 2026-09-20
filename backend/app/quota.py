"""Quota + usage ledger — two-window model.

Each request is checked against BOTH a rolling 5-hour "session" window
AND a rolling 7-day "weekly" window. Both must have budget remaining.

The two-window design mirrors what Claude does: session limits keep any
one user from burning the shared free-tier Gemini pool in a burst; the
weekly limit ties total usage to what a paid plan can recover in gross
margin.

Token counting is estimated (`len(text)//4`) — see the previous inline
justification. A metadata-driven swap doesn't require changing this
module's public surface.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from .settings import get_settings


CHARS_PER_TOKEN = 4  # rough English estimate; matches OpenAI/Gemini ballpark
WEEKLY_HOURS = 24 * 7


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def _iso_since(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _service_headers() -> dict[str, str]:
    s = get_settings()
    return {
        "apikey": s.supabase_service_role_key,
        "Authorization": f"Bearer {s.supabase_service_role_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _sum_since(user_id: str, hours: int) -> int:
    """Sum prompt+completion tokens for the last `hours`."""
    s = get_settings()
    if not s.supabase_url:
        return 0
    url = f"{s.supabase_url}/rest/v1/usage_ledger"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            url,
            params={
                "user_id": f"eq.{user_id}",
                "created_at": f"gte.{_iso_since(hours)}",
                "select": "prompt_tokens,completion_tokens",
            },
            headers=_service_headers(),
        )
    if resp.status_code != 200:
        # Fail-open on ledger read errors — better a free request than
        # a broken user experience. Errors surface in the request log
        # for triage. Flip to fail-closed once we've seen real
        # transient Supabase failure shapes in production.
        return 0
    rows = resp.json() or []
    return sum(
        int(r.get("prompt_tokens", 0) or 0)
        + int(r.get("completion_tokens", 0) or 0)
        for r in rows
    )


async def usage_windows(user_id: str, tier: str) -> dict[str, Any]:
    """Snapshot of both windows for a user. Used by /v1/usage."""
    s = get_settings()
    session_hours = s.tier_session_hours
    session_used = await _sum_since(user_id, session_hours)
    weekly_used = await _sum_since(user_id, WEEKLY_HOURS)
    session_quota = s.tier_session_quotas.get(
        tier, min(s.tier_session_quotas.values()),
    )
    weekly_quota = s.tier_weekly_quotas.get(
        tier, min(s.tier_weekly_quotas.values()),
    )
    return {
        "tier": tier,
        "session_hours": session_hours,
        "session_used_tokens": session_used,
        "session_quota_tokens": session_quota,
        "session_remaining_tokens": max(0, session_quota - session_used),
        "weekly_used_tokens": weekly_used,
        "weekly_quota_tokens": weekly_quota,
        "weekly_remaining_tokens": max(0, weekly_quota - weekly_used),
    }


async def check_quota(user_id: str, tier: str) -> None:
    """Raise 402 if either window is used up.

    We check weekly first (more permanent state); session cap is the
    fallback with a "come back in a few hours" framing.
    """
    windows = await usage_windows(user_id, tier)
    if windows["weekly_used_tokens"] >= windows["weekly_quota_tokens"]:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "quota_exceeded",
                "window": "weekly",
                "tier": tier,
                "used_tokens": windows["weekly_used_tokens"],
                "quota_tokens": windows["weekly_quota_tokens"],
                "reset_hours": WEEKLY_HOURS,
            },
        )
    if windows["session_used_tokens"] >= windows["session_quota_tokens"]:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "session_quota_exceeded",
                "window": "session",
                "tier": tier,
                "used_tokens": windows["session_used_tokens"],
                "quota_tokens": windows["session_quota_tokens"],
                "reset_hours": windows["session_hours"],
            },
        )


async def write_ledger(
    *,
    user_id: str,
    endpoint: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    status_code: int,
) -> None:
    """Insert one row. Fire-and-forget — errors do not fail the request."""
    s = get_settings()
    if not s.supabase_url:
        return
    url = f"{s.supabase_url}/rest/v1/usage_ledger"
    body: dict[str, Any] = {
        "user_id": user_id,
        "endpoint": endpoint,
        "model": model[:64],
        "prompt_tokens": max(0, int(prompt_tokens)),
        "completion_tokens": max(0, int(completion_tokens)),
        "status_code": int(status_code),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, headers=_service_headers(), json=body)
    except httpx.HTTPError:
        pass  # best-effort; a lost row is preferable to failing the response


def _history_text(history: list[dict]) -> str:
    parts = []
    for turn in history or []:
        t = turn.get("text") if isinstance(turn, dict) else None
        if t:
            parts.append(t)
    return "\n".join(parts)


def estimate_llm_tokens(
    prompt: str,
    history: list[dict],
    reply_or_result: Any,
) -> tuple[int, int]:
    """Rough (prompt_tokens, completion_tokens) split from raw text."""
    prompt_text = f"{_history_text(history)}\n{prompt}"
    if isinstance(reply_or_result, str):
        completion_text = reply_or_result
    elif isinstance(reply_or_result, dict):
        completion_text = str(reply_or_result)
    else:
        completion_text = str(reply_or_result or "")
    return estimate_tokens(prompt_text), estimate_tokens(completion_text)
