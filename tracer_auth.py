"""Tracer plugin auth — RFC 8628 Device Code Flow client.

Handles the plugin's half of the login handshake:

  1. POST /api/device/code on the Tracer web frontend, get user_code +
     device_code + verification_uri.
  2. Show the user_code to the user; open verification_uri_complete in
     their default browser.
  3. Poll /api/device/token every `interval` seconds until the user
     activates on the web (or the code expires).
  4. Persist the returned access_token so the next plugin launch skips
     the whole dance.

Kept separate from sidecar_app.py so it can be exercised without spinning
up Qt — pure `requests`, no Qt event loop, no UI.

The two service URLs are configurable via env so we can point the same
plugin binary at localhost during dev and production during ship:

  TRACER_WEB_URL   — where the device flow + /activate page live.
                     Default: https://tracer.dev (prod).
  TRACER_API_URL   — where the LLM proxy lives.
                     Default: https://api.tracer.dev (prod).
"""
from __future__ import annotations

import json
import os
import stat
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import requests

# Public prod defaults — the plugin ships pointing here. Override via env
# vars for local dev; devs set both to their localhost ports.
DEFAULT_WEB_URL = "https://tracer.dev"
DEFAULT_API_URL = "https://api.tracer.dev"

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# Optional file-based override next to this module. Solves the Windows
# "Explorer's stale env snapshot" problem: process env vars require the
# launching parent to have them set at launch time; a file next to the
# plugin is always visible. Format is dead-simple KEY=VALUE per line.
_ENV_FILE = Path(__file__).resolve().parent / ".tracer.env"


def _read_env_file() -> Dict[str, str]:
    if not _ENV_FILE.is_file():
        return {}
    out: Dict[str, str] = {}
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _configured(key: str, default: str) -> str:
    # Precedence: process env → .tracer.env file next to this module → default.
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    return _read_env_file().get(key, default)


def web_url() -> str:
    return _configured("TRACER_WEB_URL", DEFAULT_WEB_URL).rstrip("/")


def api_url() -> str:
    return _configured("TRACER_API_URL", DEFAULT_API_URL).rstrip("/")


# ---------------------------------------------------------------------------
# JWT storage — one JSON file in %APPDATA%\Tracer (or ~/.config/tracer)
# ---------------------------------------------------------------------------

def _token_path() -> Path:
    """Where the JWT lives. Override with TRACER_TOKEN_PATH for tests."""
    override = os.environ.get("TRACER_TOKEN_PATH")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Tracer" / "token.json"
    return Path.home() / ".config" / "tracer" / "token.json"


def load_token() -> Optional[Dict[str, Any]]:
    """Return the saved token dict or None."""
    p = _token_path()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_token(payload: Dict[str, Any]) -> None:
    """Persist to a 0600 file. `payload` should include access_token, expires_in."""
    p = _token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Best-effort: tighten perms on POSIX; on Windows we rely on APPDATA
    # already being the user's private profile dir.
    if os.name != "nt":
        try:
            p.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


def clear_token() -> None:
    p = _token_path()
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Device code flow
# ---------------------------------------------------------------------------

class DeviceFlow:
    """One instance per login attempt. Cancellable, thread-safe."""

    def __init__(
        self,
        *,
        on_pending: Callable[[Dict[str, Any]], None],
        on_success: Callable[[Dict[str, Any]], None],
        on_error: Callable[[str], None],
        open_browser: bool = True,
    ) -> None:
        self._on_pending = on_pending
        self._on_success = on_success
        self._on_error = on_error
        self._open_browser = open_browser
        self._cancelled = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Kick off the flow on a daemon thread. Returns immediately."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="tracer-auth")
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled.set()

    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            code_resp = self._mint_code()
        except Exception as exc:  # noqa: BLE001
            self._on_error(f"could not reach {web_url()}: {exc}")
            return
        if self._cancelled.is_set():
            return

        # Tell the UI: here's the user_code, here's where to activate it.
        self._on_pending(code_resp)

        if self._open_browser and code_resp.get("verification_uri_complete"):
            try:
                webbrowser.open(code_resp["verification_uri_complete"])
            except Exception:  # noqa: BLE001
                # Not fatal — user can copy from the UI.
                pass

        try:
            token = self._poll(code_resp)
        except Exception as exc:  # noqa: BLE001
            self._on_error(f"poll failed: {exc}")
            return
        if token is None:
            # Cancelled or expired; the specific state is already surfaced.
            return

        save_token(token)
        self._on_success(token)

    def _mint_code(self) -> Dict[str, Any]:
        r = requests.post(f"{web_url()}/api/device/code", timeout=10)
        r.raise_for_status()
        return r.json()

    def _poll(self, code_resp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        device_code = code_resp["device_code"]
        interval = int(code_resp.get("interval", 5))
        deadline = time.time() + int(code_resp.get("expires_in", 900))

        while time.time() < deadline:
            if self._cancelled.is_set():
                return None
            time.sleep(interval)
            if self._cancelled.is_set():
                return None
            try:
                r = requests.post(
                    f"{web_url()}/api/device/token",
                    json={"grant_type": DEVICE_GRANT_TYPE, "device_code": device_code},
                    timeout=10,
                )
            except Exception as exc:  # noqa: BLE001
                # Network hiccup — keep polling until the code itself expires.
                self._on_error(f"network: {exc}")
                continue
            if r.status_code == 200:
                return r.json()
            # 4xx bodies carry the OAuth error name per RFC 8628 §3.5.
            try:
                body = r.json()
            except ValueError:
                body = {"error": f"http_{r.status_code}"}
            err = body.get("error", "unknown")
            if err == "authorization_pending":
                continue  # still waiting for user to activate
            if err == "slow_down":
                interval += 5
                continue
            if err in ("expired_token", "access_denied", "invalid_grant"):
                self._on_error(err)
                return None
            # Anything else — surface and stop.
            self._on_error(err)
            return None

        self._on_error("expired_token")
        return None


# ---------------------------------------------------------------------------
# Runnable check — `python tracer_auth.py` walks the flow against localhost.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.environ.setdefault("TRACER_WEB_URL", "http://localhost:3000")

    def _pending(info: Dict[str, Any]) -> None:
        print(f"\n1. Open: {info.get('verification_uri_complete', info.get('verification_uri'))}")
        print(f"2. Paste: {info.get('user_code')}")
        print("3. Waiting…")

    def _success(tok: Dict[str, Any]) -> None:
        print(f"\n✓ access_token stored at {_token_path()}")
        print(f"  expires_in: {tok.get('expires_in')}s")

    def _error(msg: str) -> None:
        print(f"✗ {msg}")

    flow = DeviceFlow(on_pending=_pending, on_success=_success, on_error=_error)
    flow.start()
    if flow._thread is not None:
        flow._thread.join()

    # Verify by hitting /v1/me on the API.
    saved = load_token()
    assert saved is not None, "login must save a token"
    api = os.environ.get("TRACER_API_URL", "http://127.0.0.1:8100").rstrip("/")
    r = requests.get(
        f"{api}/v1/me",
        headers={"Authorization": f"Bearer {saved['access_token']}"},
        timeout=10,
    )
    print(f"\n/v1/me → {r.status_code}: {r.text}")
    assert r.status_code == 200, "/v1/me must accept the freshly minted token"
    print("OK")
