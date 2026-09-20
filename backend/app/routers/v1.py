"""/v1/* — LLM proxy endpoints backed by agent.py.

Contract mirrors the plugin's current agent shape so the plugin can switch
from local direct-Gemini calls to backend HTTP with minimal downstream
changes: same request payload, same response shape.

Every route:
  1. Requires a valid device JWT via AuthedUser.
  2. Passes quota check (Slice F) — 402 if the user's tier monthly
     budget is used up.
  3. Runs the agent call.
  4. Writes a usage_ledger row with estimated token counts.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .. import agent, quota
from ..auth import AuthedUser

router = APIRouter(prefix="/v1", tags=["llm"])

_MODEL_FALLBACK = "gemini-2.5-pro"  # for ledger rows when agent.py hasn't picked one


def _current_model() -> str:
    return (getattr(agent, "_working_model", None)
            or getattr(agent, "MODEL_CANDIDATES", [_MODEL_FALLBACK])[0])


# ---------------------------------------------------------------------------
# Request models — the plugin sends the same shape it currently constructs
# in-process. History is a list of {role, text} turns.
# ---------------------------------------------------------------------------

class Turn(BaseModel):
    role: str  # "user" | "model"
    text: str


class Attachment(BaseModel):
    """Attachments were paths on the local plugin. From the cloud they
    arrive as base64 data URIs — but Slice E doesn't wire attachments yet.
    Added the field so the plugin can start sending them without a schema
    break; the backend just ignores them for now.
    """
    name: str
    data_url: str | None = None


class GenerateRequest(BaseModel):
    prompt: str
    history: list[Turn] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)


class AskRequest(BaseModel):
    prompt: str
    history: list[Turn] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/generate")
async def generate(body: GenerateRequest, user: AuthedUser) -> dict[str, Any]:
    """Agent mode. Runs the full generate → backend build → auto-repair loop."""
    await quota.check_quota(user.id, user.tier)
    history = [turn.model_dump() for turn in body.history]
    # Attachments deferred (Slice E scope): images require an upload/URI
    # design decision. For now the plugin can still send them; agent.py
    # will just see an empty attachments list here.
    try:
        result = await run_in_threadpool(
            agent.build_with_auto_repair,
            body.prompt, history=history, attachments=None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"[v1/generate] agent failed:\n{tb}", flush=True)
        await _write_ledger_error(user.id, "/v1/generate", body.prompt, history, 500)
        raise HTTPException(status_code=500, detail={
            "error": "agent_failed",
            "message": str(exc)[:500],
            "type": type(exc).__name__,
        })
    prompt_tokens, completion_tokens = quota.estimate_llm_tokens(
        body.prompt, history, result,
    )
    await quota.write_ledger(
        user_id=user.id,
        endpoint="/v1/generate",
        model=_current_model(),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        status_code=200,
    )
    return result


@router.post("/ask")
async def ask(body: AskRequest, user: AuthedUser) -> dict[str, Any]:
    """Ask mode — plain-text Q&A."""
    await quota.check_quota(user.id, user.tier)
    history = [turn.model_dump() for turn in body.history]
    try:
        reply = await run_in_threadpool(agent.ask_gemini, body.prompt, history)
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"[v1/ask] agent failed:\n{tb}", flush=True)
        await _write_ledger_error(user.id, "/v1/ask", body.prompt, history, 500)
        raise HTTPException(status_code=500, detail={
            "error": "agent_failed",
            "message": str(exc)[:500],
            "type": type(exc).__name__,
        })
    prompt_tokens, completion_tokens = quota.estimate_llm_tokens(
        body.prompt, history, reply,
    )
    await quota.write_ledger(
        user_id=user.id,
        endpoint="/v1/ask",
        model=_current_model(),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        status_code=200,
    )
    return {"reply": reply, "user_id": user.id, "tier": user.tier}


@router.post("/plan")
async def plan(body: AskRequest, user: AuthedUser) -> dict[str, Any]:
    """Plan mode — design outline, no build."""
    await quota.check_quota(user.id, user.tier)
    history = [turn.model_dump() for turn in body.history]
    try:
        reply = await run_in_threadpool(agent.plan_gemini, body.prompt, history)
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"[v1/plan] agent failed:\n{tb}", flush=True)
        await _write_ledger_error(user.id, "/v1/plan", body.prompt, history, 500)
        raise HTTPException(status_code=500, detail={
            "error": "agent_failed",
            "message": str(exc)[:500],
            "type": type(exc).__name__,
        })
    prompt_tokens, completion_tokens = quota.estimate_llm_tokens(
        body.prompt, history, reply,
    )
    await quota.write_ledger(
        user_id=user.id,
        endpoint="/v1/plan",
        model=_current_model(),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        status_code=200,
    )
    return {"reply": reply, "user_id": user.id, "tier": user.tier}


@router.get("/me")
async def me(user: AuthedUser) -> dict[str, Any]:
    """Sanity endpoint — plugin can hit this after login to confirm the JWT."""
    return {"id": user.id, "tier": user.tier, "jti": user.jti}


@router.get("/usage")
async def usage(user: AuthedUser) -> dict[str, Any]:
    """Two-window usage summary — session (rolling 5h) + weekly (rolling 7d).

    The plugin and the /billing dashboard both read this. Shape stays stable
    so downstream doesn't have to branch on whether the session window is
    the tight one or the weekly is.
    """
    return await quota.usage_windows(user.id, user.tier)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _write_ledger_error(
    user_id: str, endpoint: str, prompt: str, history: list, status_code: int,
) -> None:
    """Record the failed-request footprint. Prompt tokens still cost even
    when the agent errored — we want visibility on repeated failures."""
    prompt_tokens = quota.estimate_tokens(prompt) + quota.estimate_tokens(
        quota._history_text(history)  # noqa: SLF001
    )
    await quota.write_ledger(
        user_id=user_id,
        endpoint=endpoint,
        model=_current_model(),
        prompt_tokens=prompt_tokens,
        completion_tokens=0,
        status_code=status_code,
    )
