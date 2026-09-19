"""/v1/* — LLM proxy endpoints backed by agent.py.

Contract mirrors the plugin's current agent shape so the plugin can switch
from local direct-Gemini calls to backend HTTP with minimal downstream
changes: same request payload, same response shape.

Every route requires a valid device JWT via AuthedUser.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import agent
from ..auth import AuthedUser

router = APIRouter(prefix="/v1", tags=["llm"])


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
    """Agent mode. Runs the full generate → backend build → auto-repair loop.

    Slice E returns whatever `build_with_auto_repair` returns; the plugin
    already knows how to consume it (status: success | clarification_needed
    | backend_rejected).
    """
    history = [turn.model_dump() for turn in body.history]
    # Attachments deferred (Slice E scope): images require an upload/URI
    # design decision. For now the plugin can still send them; agent.py
    # will just see an empty attachments list here.
    result = agent.build_with_auto_repair(
        body.prompt, history=history, attachments=None,
    )
    return result


@router.post("/ask")
async def ask(body: AskRequest, user: AuthedUser) -> dict[str, Any]:
    """Ask mode — plain-text Q&A."""
    history = [turn.model_dump() for turn in body.history]
    reply = agent.ask_gemini(body.prompt, history)
    return {"reply": reply, "user_id": user.id, "tier": user.tier}


@router.post("/plan")
async def plan(body: AskRequest, user: AuthedUser) -> dict[str, Any]:
    """Plan mode — design outline, no build."""
    history = [turn.model_dump() for turn in body.history]
    reply = agent.plan_gemini(body.prompt, history)
    return {"reply": reply, "user_id": user.id, "tier": user.tier}


@router.get("/me")
async def me(user: AuthedUser) -> dict[str, Any]:
    """Sanity endpoint — plugin can hit this after login to confirm the JWT."""
    return {"id": user.id, "tier": user.tier, "jti": user.jti}
