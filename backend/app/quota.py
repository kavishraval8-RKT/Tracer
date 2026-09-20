"""Quota + usage ledger — Slice F.

The backend meters every /v1/* request. Two Supabase touches:

  1. Before the LLM call — `check_quota(user)` sums this month's
     ledger rows and refuses with 402 when the tier budget is used up.
  2. After the LLM call — `write_ledger(...)` inserts one row with
     estimated token counts.

Token counting is estimated (`len(text)//4`) rather than pulled from
Gemini's `resp.usage_metadata` because:

  * agent.py's public surface returns str / dict, not the raw resp.
    Plumbing the metadata out would touch every LLM call site.
  * The estimation error (~±30%) is well below quota granularity
    (tier steps are 5x apart).
  * A future refactor can swap to metadata-driven counting without
    changing this module's public surface.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from .settings import get_settings


CHARS_PER_TOKEN = 4  # rough English estimate; matches OpenAI/Gemini ballpark


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def _month_start_iso() -> str:
    """ISO timestamp for the first millisecond of the current month (UTC)."""
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


def _service_headers() -> dict[str, str]:
    s = get_settings()
    return {
        "apikey": s.supabase_service_role_key,
        "Authorization": f"Bearer {s.supabase_service_role_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _month_usage_tokens(user_id: str) -> int:
    """Sum prompt_tokens + completion_tokens for the current month."""
    s = get_settings()
    if not s.supabase_url:
        return 0
    url = f"{s.supabase_url}/rest/v1/usage_ledger"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            url,
            params={
                "user_id": f"eq.{user_id}",
                "created_at": f"gte.{_month_start_iso()}",
                "select": "prompt_tokens,completion_tokens",
            },
            headers=_service_headers(),
        )
    if resp.status_code != 200:
        # Fail-open on ledger read errors — better a free request than
        # a broken user experience. Errors will surface in the request
        # log for triage. Flip to fail-closed once we've seen the shape
        # of transient Supabase errors in production.
        return 0
    rows = resp.json() or []
    return sum(int(r.get("prompt_tokens", 0) or 0)
               + int(r.get("completion_tokens", 0) or 0)
               for r in rows)


async def check_quota(user_id: str, tier: str) -> None:
    """Raise 402 if the user is over their tier's monthly budget."""
    s = get_settings()
    quota = s.tier_quotas.get(tier)
    if quota is None:
        # Unknown tier — treat as most restrictive. Don't crash.
        quota = min(s.tier_quotas.values())
    used = await _month_usage_tokens(user_id)
    if used >= quota:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "quota_exceeded",
                "tier": tier,
                "used_tokens": used,
                "quota_tokens": quota,
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
        "model": model[:64],  # column cap
        "prompt_tokens": max(0, int(prompt_tokens)),
        "completion_tokens": max(0, int(completion_tokens)),
        "status_code": int(status_code),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, headers=_service_headers(), json=body)
    except httpx.HTTPError:
        # Best-effort: the LLM call already succeeded; a lost ledger row
        # is preferable to failing the response.
        pass


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
        # Agent-mode dict — approximate by size of the whole payload.
        completion_text = str(reply_or_result)
    else:
        completion_text = str(reply_or_result or "")
    return estimate_tokens(prompt_text), estimate_tokens(completion_text)
