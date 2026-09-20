"""Tracer plugin HTTP client — Slice I.

Replaces the local ``agent.build_with_auto_repair`` / ``ask_gemini`` /
``plan_gemini`` calls with POSTs to the cloud backend. Same request
shape, same response shape — designed as a drop-in swap.

Auth
----
Every call includes the device JWT saved by ``tracer_auth`` in the
``Authorization: Bearer …`` header. If the backend returns 401 (revoked
or expired), we clear the stored token so the sidecar's next
push_auth_state() emits ``logged_out`` and the chat overlay reappears.

Error taxonomy
--------------
Callers get one of these exceptions on failure — the sidecar's
_on_generate_failed() branches on them:

* ``NotAuthenticatedError`` — 401. Local token also cleared.
* ``QuotaExceededError``    — 402 (Slice F territory; here for future use).
* ``CloudUnreachableError`` — network / DNS / 5xx / non-JSON body.

Non-Slice-I features intentionally deferred
-------------------------------------------
* Streaming (SSE). Backend doesn't stream yet; we wait for the full
  response like the local path did.
* Attachments upload. Backend hasn't wired multimodal parts across HTTP.
  For now the plugin still sends the array (backend ignores it).
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

import requests

import tracer_auth


TIMEOUT_S = 120.0  # generate can take a while with the repair loop


class NotAuthenticatedError(RuntimeError):
    """401 from the backend. Token was cleared before this was raised."""


class QuotaExceededError(RuntimeError):
    """402 from the backend."""


class CloudUnreachableError(RuntimeError):
    """Network layer failure, 5xx, or garbled response body."""


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _api_url() -> str:
    return os.environ.get("TRACER_API_URL", tracer_auth.DEFAULT_API_URL).rstrip("/")


def _bearer() -> str:
    tok = tracer_auth.load_token()
    if not tok or not tok.get("access_token"):
        raise NotAuthenticatedError("no saved token")
    return tok["access_token"]


def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST to the backend with the Bearer token; classify failures."""
    token = _bearer()
    try:
        resp = requests.post(
            f"{_api_url()}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_S,
        )
    except requests.exceptions.RequestException as exc:
        raise CloudUnreachableError(f"network: {exc}") from exc

    if resp.status_code == 401:
        # Revoked or expired. Clear the token so the shell drops to
        # logged_out on the next auth-state push.
        tracer_auth.clear_token()
        try:
            detail = resp.json().get("detail", {})
        except ValueError:
            detail = {}
        raise NotAuthenticatedError(detail.get("error") or f"http_{resp.status_code}")

    if resp.status_code == 402:
        try:
            detail = resp.json().get("detail", {})
        except ValueError:
            detail = {}
        used = detail.get("used_tokens")
        quota_tokens = detail.get("quota_tokens")
        tier = detail.get("tier", "hobby")
        if used is not None and quota_tokens is not None:
            msg = (f"{tier} tier: used {used:,} / {quota_tokens:,} tokens this month")
        else:
            msg = detail.get("error") or "quota_exceeded"
        raise QuotaExceededError(msg)

    if resp.status_code >= 500:
        raise CloudUnreachableError(f"backend {resp.status_code}: {resp.text[:200]}")

    try:
        return resp.json()
    except ValueError:
        raise CloudUnreachableError(
            f"backend returned non-JSON (status {resp.status_code}): {resp.text[:200]}"
        )


# ---------------------------------------------------------------------------
# Public API — mirrors agent.py's public function signatures so the
# sidecar workers can swap `agent` for `tracer_api` with minimal change.
# ---------------------------------------------------------------------------

def build_with_auto_repair(
    prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    log: Optional[Callable[[str], None]] = None,
    attachments: Optional[List[Dict[str, str]]] = None,
    max_repairs: int = 2,  # accepted for signature compatibility; ignored (backend owns)
) -> Dict[str, Any]:
    """Cloud version of ``agent.build_with_auto_repair``.

    The backend runs the same auto-repair loop internally; we get back a
    result dict with the same shape (status / circuit / backend / history).
    """
    if log:
        log(f"Asking {_api_url()}/v1/generate …")
    return _post("/v1/generate", {
        "prompt": prompt,
        "history": history or [],
        "attachments": [{"name": a.get("name", "?"), "data_url": None}
                        for a in (attachments or [])],
    })


def ask_gemini(
    prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    attachments: Optional[List[Dict[str, str]]] = None,
) -> str:
    body = _post("/v1/ask", {
        "prompt": prompt,
        "history": history or [],
    })
    return body.get("reply") or "(no reply)"


def plan_gemini(
    prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    attachments: Optional[List[Dict[str, str]]] = None,
) -> str:
    body = _post("/v1/plan", {
        "prompt": prompt,
        "history": history or [],
    })
    return body.get("reply") or "(no reply)"


# ---------------------------------------------------------------------------
# Runnable check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Requires: TRACER_API_URL set, uvicorn on that URL, and
    # tracer_auth.load_token() returning a valid saved token.
    os.environ.setdefault("TRACER_API_URL", "http://127.0.0.1:8100")
    print("api_url:", _api_url())
    print("has token:", tracer_auth.load_token() is not None)

    try:
        reply = ask_gemini("one sentence: what does a bypass capacitor do?")
        print("\n/v1/ask reply:\n ", reply)
        print("\nOK")
    except NotAuthenticatedError as exc:
        print(f"✗ auth: {exc}")
    except QuotaExceededError as exc:
        print(f"✗ quota: {exc}")
    except CloudUnreachableError as exc:
        print(f"✗ cloud: {exc}")
