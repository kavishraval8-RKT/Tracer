"""Tracer agent -- natural language in, KiCad schematic out.

Sits in front of `app.py`: a prompt goes to Gemini, which answers either with a
circuit block in the engine's JSON schema or with a clarifying question, and a
finished block is POSTed straight to the running backend.

The Gemini key comes from the GEMINI_API_KEY environment variable, or from a
.env file beside this script. Never from source. See .env.example.

    uvicorn app:app --reload --port 8000      # in another terminal
    python agent.py

Three entry points:

    python agent.py --check-key               says where the key was found and
                                              whether Gemini accepts it; exits
                                              0 on success, 1 on failure
    python agent.py "a 555 astable at 1 Hz"   interactive; asks follow-ups at
                                              the console and waits for answers
    python agent.py --json                    one JSON request on stdin, one
                                              JSON response on stdout, no
                                              prompting -- this is what the
                                              KiCad Copilot plugin drives

The backend does the real validation, so this module deliberately does very
little checking of its own: it normalises the shape Gemini returns, hands it
over, and reports what came back.
"""

from __future__ import annotations

import json
import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import warnings
warnings.filterwarnings("ignore")
import google.generativeai as genai
import requests

#: This script's own directory -- where a .env file is looked for.
HERE = Path(__file__).resolve().parent

# --- configuration -----------------------------------------------------------

# The key is never stored in this file. It comes from the GEMINI_API_KEY
# environment variable, or from a .env file beside this one -- see .env.example.
# Nothing here carries a default: a missing key must fail loudly rather than
# quietly fall back to someone else's quota.

# Free-tier quota is counted per model per day (20 requests), so switching
# models is the quickest way past a daily cap. As of the last test run
# gemini-3.6-flash and gemini-3.7-flash were both spent; this one was not.
# Ranked best -> worst reasoning ability. Each has its own daily free-tier
# quota (20 req/day), so we chain them: on 404/quota-exhausted, drop and
# try the next. First success is cached in _working_model for the session.
MODEL_CANDIDATES: List[str] = [
    "gemini-2.5-pro",           # strongest reasoning, small quota
    "gemini-3.8-flash",         # newest flash
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",      # alias fallback
]
MODEL_NAME = MODEL_CANDIDATES[0]  # legacy alias; the actual pick comes from _choose_model()

_failed_models: set = set()
_working_model: Optional[str] = None
BACKEND_URL = "http://127.0.0.1:8000/api/v1/generate"
REQUEST_TIMEOUT_S = 120

# How many clarification rounds the console entry point will sit through before
# giving up. The plugin drives its own loop and ignores this.
MAX_CLARIFICATIONS = 4

# The layout engine rejects a geometry it cannot route. Handing the complaint
# back to Gemini fixes it often enough to be worth the round trip.
MAX_REPAIRS = 2


#: Parsed once; a long plugin session should not re-stat the file per request.
_DOTENV_CACHE: Optional[Dict[str, str]] = None

#: What .env.example ships with. Treated as "no key", so a tester who copied
#: the file but never pasted a key gets our message rather than a 400 from
#: Google several seconds later.
_KEY_PLACEHOLDER = "your_gemini_api_key_here"

_NO_KEY_MESSAGE = """\
No Gemini API key found.

Set it for this shell:
    PowerShell:  $env:GEMINI_API_KEY = "<your key>"
    bash/zsh:    export GEMINI_API_KEY=<your key>

Or persist it: copy .env.example to .env and put your key in it.
Expected at: {dotenv}

Get a key from https://aistudio.google.com/apikey"""


def _load_dotenv() -> Dict[str, str]:
    """Parse a .env file beside this script, if there is one.

    Hand-rolled rather than pulling in python-dotenv: it is a dozen lines, and
    the plugin runs this agent in whichever interpreter happens to carry the
    Gemini SDK, so every extra dependency is one more thing a tester has to
    install into exactly the right Python.

    Deliberately does NOT write into os.environ. A real environment variable
    should always win, and mutating the process environment would make which
    one wins depend on call order.
    """
    global _DOTENV_CACHE
    if _DOTENV_CACHE is not None:
        return _DOTENV_CACHE

    values: Dict[str, str] = {}
    try:
        lines = (HERE / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        # Strip one layer of matching quotes, the way a shell would.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", chr(34)):
            value = value[1:-1]
        values[name.strip()] = value

    _DOTENV_CACHE = values
    return values


def _api_key() -> str:
    """The Gemini key, from the environment or .env. Never from source."""
    key = (os.environ.get("GEMINI_API_KEY")
           or _load_dotenv().get("GEMINI_API_KEY")
           or "").strip()
    if not key or key == _KEY_PLACEHOLDER:
        raise RuntimeError(_NO_KEY_MESSAGE.format(dotenv=HERE / ".env"))
    return key


# --- system instruction ------------------------------------------------------
#
# The schema is spelled out in full rather than summarised: the backend forbids
# unknown keys inside `block`, so an invented field is a hard 422 rather than
# something the engine can shrug off.

SYSTEM_INSTRUCTION = """\
You are Tracer, a schematic-capture engine with a conversational front end. You
turn a natural-language circuit description into one JSON object that drives a
KiCad generator. You output JSON only -- no prose, no markdown, no code fences,
no comments.

# You return exactly one of two shapes

## Option A -- you have enough to build

{
  "status": "success",
  "circuit": {
    "name": "<short human name for the circuit>",
    "sheet": "A4",
    "grid_mm": 1.27,
    "layout": { "channel_mm": 15.24, "body_margin_mm": 3.81 },
    "components": [
      {
        "ref": "R1",
        "value": "330",
        "symbol": "Device:R",
        "footprint": "Resistor_SMD:R_0603_1608Metric",
        "column": 3,
        "row": -1,
        "rotation": 90,
        "role": "controller"
      }
    ],
    "nets": [
      {
        "name": "+3V3",
        "class": "power",
        "connections": [ { "ref": "U1", "pin": "8" } ]
      }
    ]
  }
}

## Option B -- one detail genuinely blocks you

{
  "status": "clarification_needed",
  "question": "What supply voltage should the sensor rail run at -- 3.3 V or 5 V?"
}

# When to ask, and when not to

Asking costs the user a round trip, so ask only when the answer changes what
parts go on the sheet and you cannot pick a sane default:
- ASK when a required part is unnamed and the choice is wide open ("a sensor",
  "a motor driver", "some display") -- you need the actual part.
- ASK when the supply rail is unstated and the circuit's parts depend on it.
- ASK when two readings of the request give materially different schematics.
- DO NOT ask about values you can reasonably default (a decoupling cap is
  100 nF, an LED series resistor is 330 ohm, a reset pull-up is 10k).
- DO NOT ask about footprints, sheet size, or layout -- choose them yourself.
- DO NOT ask more than one thing at a time. One question, one sentence.
- Once the user has answered, BUILD. Do not ask a second question about a
  detail you could have defaulted.

Earlier turns in this conversation are the user's original request and their
answers to your questions. Treat the whole conversation as the specification:
carry every constraint from the first prompt into the circuit you finally emit,
not just the most recent answer.

# Field rules -- the backend enforces every one of these strictly

- Emit exactly the keys shown. Any extra key inside "circuit" is rejected.
- "ref" is a standard designator (R1, C1, U1, D1, J1, TP1, Q1, SW1) and is
  unique across the whole block.
- "symbol" must read "Library:SymbolName" -- exactly one colon.
- "rotation" is optional and must be one of 0, 90, 180, 270.
- "role" is optional; at most ONE component may carry "role": "controller".
- "pin" is the symbol's own pin number, written as a string ("1", "8"). It is
  not a sequential index -- use the real part's pin numbering.
- "class" is "power" for rails (VIN, +5V, +3V3, GND) and "signal" for
  everything else. Power nets are drawn as labels, signal nets as wires.
- Every "ref" inside "nets" must exist in "components". Net names are unique,
  and no net lists the same (ref, pin) pair twice.
- Omit "footprint" and "rotation" rather than guessing a wrong value; never
  emit null.
- DO emit "layout" on every build, and start from the roomy defaults:
  "channel_mm": 15.24 and "body_margin_mm": 3.81. `channel_mm` is the clear
  routing corridor between adjacent columns and `body_margin_mm` is the
  keep-out around each symbol, so both buy the router room to get around a
  part instead of failing. Raise them (up to about 25.4 and 7.62) for a dense
  block or after a routing rejection; never lower them below these defaults.

# Layout rules

Placement is derived from (column, row), so those two numbers carry the whole
layout. You never write absolute coordinates: the engine already places columns
strictly left to right in ascending order and pins the controller dead centre
on the sheet, so your whole job here is choosing a sensible column, row and
rotation for each part -- plus the "layout" spacing numbers above.
- "column" is the signal-flow stage. Ascending column = strict left-to-right
  placement. Power enters at column 0 and the signal flows rightwards:
  connector/input -> regulation -> controller -> passives -> outputs.
- "row" is the vertical slot within a column. Row 0 is the sheet centre line,
  negative is above it, positive below.
- The main controller (the MCU, op-amp, or whatever the circuit is built
  around) carries "role": "controller", sits at row 0, and goes in a middle
  column so the sheet stays balanced -- inputs to its left, outputs to its
  right. The engine pins it dead centre on the sheet.
- Keep the controller's own slot clear: put its decoupling caps and pull-ups
  one row above or below, never at (controller column, row 0).
- Spread parts across rows instead of stacking a column deep; keep each column
  to roughly four rows.
- Use "rotation" to aim pins along the signal flow: 90 or 270 for a series
  resistor in a vertical branch, 180 for a part that feeds leftwards.

# Routing -- keeping the layout buildable

The engine routes real orthogonal wires around symbol bodies and REJECTS any
layout it cannot find a clear path through, so keep the sheet roomy:
- Keep a block SMALL. Twelve components is comfortable on A4; past that the
  columns run off the sheet and the build fails outright. Split a big design
  rather than cramming it.
- Keep each column to about three parts, spread over rows -2..+2.
- A two-pin part (R, C, L, LED, D) sits VERTICAL at rotation 0, with pin 1
  above the anchor and pin 2 below. Rotation 90 lays it flat with pin 1 to the
  left. Use rotation 90 or 270 for a part a signal passes through
  horizontally, and leave rotation off for one hanging between a rail and
  ground.
- Put the two ends of a signal net near each other: the part driving a
  component belongs in the immediately preceding column, in the same row or
  one row off.
- Large symbols (an MCU, a 555, an op-amp) need their neighbours kept clear;
  do not park a part directly against one in the next column.
- Get two-pin polarity right, because it decides which way the part faces.
  On Device:LED and Device:D pin 1 is the CATHODE and pin 2 is the ANODE, so
  current flows 2 -> 1: the driving resistor lands on pin 2 and pin 1 goes to
  the rail or to ground. Device:C_Polarized pin 1 is the positive plate.

# A worked example that builds cleanly

This exact layout passes every geometry and connectivity check. Copy its
habits -- the rotations especially.

  J1  Connector_Generic:Conn_01x02  column 0  row  0  rotation 180
  U2  Regulator_Linear:AMS1117-3.3  column 1  row  0
  C1  Device:C                      column 1  row -1
  C2  Device:C                      column 1  row  1
  U1  MCU_Microchip_ATtiny:ATtiny85-20P column 2 row 0   role controller
  C3  Device:C                      column 2  row  1
  R1  Device:R                      column 3  row -1  rotation 90
  R2  Device:R                      column 3  row  0  rotation 90
  R3  Device:R                      column 3  row  1  rotation 270
  D1  Device:LED                    column 4  row -1  rotation 180
  D2  Device:LED                    column 4  row  0  rotation 180

Note R2: same row as the controller, one column right, rotated 90 so the
signal passes straight through it. That is the pattern to reuse.

# Component rules

Use real, standard KiCad library symbol names. Common ones:
- Device:R, Device:R_Small, Device:C, Device:C_Small, Device:C_Polarized
- Device:LED, Device:D, Device:D_Schottky, Device:D_Zener
- Device:L, Device:Crystal, Device:Q_NPN_BEC, Device:Q_NMOS_GDS
- Device:R_POT, Device:Thermistor, Device:Buzzer, Device:Fuse
- MCU_Microchip_ATtiny:ATtiny85-20P, MCU_Microchip_ATmega:ATmega328P-PU
- MCU_Module:Arduino_UNO_R3, MCU_Espressif:ESP32-WROOM-32
- Regulator_Linear:AMS1117-3.3, Regulator_Linear:LM7805_TO220
- Amplifier_Operational:LM358, Amplifier_Operational:TL072
- Connector_Generic:Conn_01x02, Connector_Generic:Conn_01x03,
  Connector_Generic:Conn_01x04, Connector:TestPoint
- Switch:SW_Push, Switch:SW_SPDT
- Timer:NE555P, Timer:LM555xN
If you are not certain a symbol exists, fall back to a generic Device: part
rather than inventing a library name.

Always add the supporting parts a real schematic needs: a decoupling capacitor
on each IC supply pin, a pull-up on an active-low reset, and a series resistor
on every LED. Give every component a sensible "value".
"""


# --- conversation ------------------------------------------------------------
#
# History is carried as a plain list of {"role", "text"} dicts rather than the
# SDK's own objects, so the KiCad plugin can ship it across a subprocess
# boundary as JSON and hand it straight back on the next turn.


def _to_contents(
    history: List[Dict[str, str]],
    attachments: Optional[List[Dict[str, str]]] = None,
) -> List[Any]:
    """Turn portable history into the SDK's `contents` list.

    When `attachments` is present, PIL.Image parts are appended to the LAST
    user turn's parts list. The old google.generativeai SDK accepts a
    mixed list of dicts and PIL.Image objects in a Content's parts.
    """
    contents: List[Any] = []
    for turn in history:
        role = "model" if turn.get("role") == "model" else "user"
        contents.append({"role": role, "parts": [{"text": turn.get("text", "")}]})
    if attachments and contents and contents[-1]["role"] == "user":
        try:
            from PIL import Image  # noqa: WPS433 — deferred; Pillow is a Gemini dep
        except ImportError:
            return contents
        for att in attachments:
            path = att.get("path")
            if not path:
                continue
            try:
                img = Image.open(path)
                img.load()  # eager decode so file handle can close
                # Cap dimensions — cost + latency scale with pixels.
                img.thumbnail((1024, 1024))
                contents[-1]["parts"].append(img)
            except Exception:
                # Best-effort: silently skip a bad attachment. Sidecar
                # already surfaced the file name in Activity so the user
                # can see something's off if the reply seems oblivious.
                pass
    return contents


def _log_noop(_: str) -> None:
    pass


# --- Gemini ------------------------------------------------------------------


def _readable_api_error(exc: Exception, model: Optional[str] = None) -> RuntimeError:
    """Reduce an SDK exception to one line a user can act on.

    Quota and auth failures arrive as a page of protobuf, which buries the one
    sentence that matters -- and it is the Copilot log that has to show it.

    `model` names the model the call actually used; it defaults to MODEL_NAME
    but must be passed when probing some other model, or the message will name
    the wrong one.
    """
    text = str(exc)
    name = type(exc).__name__
    model = model or MODEL_NAME

    if "429" in text or "ResourceExhausted" in name or "quota" in text.lower():
        delay = ""
        match = re.search(r"retry_delay\s*\{\s*seconds:\s*(\d+)", text)
        if match:
            delay = f" Retry in about {match.group(1)}s."
        per_day = "PerDay" in text or "per day" in text.lower()
        return RuntimeError(
            f"Gemini rate limit reached for {model}."
            + (" This is the free tier's daily cap, so it resets tomorrow "
               "unless billing is enabled. A different model has its own "
               "quota -- change MODEL_NAME in agent.py to switch."
               if per_day else delay)
        )

    if "404" in text and "model" in text.lower():
        return RuntimeError(
            f"Gemini has no model called {model!r} for this key. "
            f"Change MODEL_NAME at the top of agent.py.")

    if "API key" in text or "PermissionDenied" in name or "401" in text:
        return RuntimeError(
            "Gemini rejected the API key. Check the GEMINI_API_KEY environment "
            "variable, or the key in your .env file. Keys are issued at "
            "https://aistudio.google.com/apikey")

    first_line = text.strip().splitlines()[0] if text.strip() else name
    return RuntimeError(f"{name}: {first_line[:300]}")


def _salvage_json(raw: str) -> Optional[Dict[str, Any]]:
    """Try to parse malformed JSON Gemini occasionally returns.

    Common failures: wrapped in ```json fences, trailing commas before ]/},
    truncated at max_output_tokens. Returns None if none of the repairs work
    -- caller rotates to a different model.
    """
    import re
    s = raw.strip()
    # Strip markdown code fences the model sometimes adds despite JSON mode.
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    # Trailing commas before ] or }.
    s = re.sub(r",\s*([\]}])", r"\1", s)
    # First attempt on the cleaned string.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Truncated payload -- try lopping off after the last balanced close.
    depth = 0
    last_ok = -1
    in_str = False
    esc = False
    for i, ch in enumerate(s):
        if esc:
            esc = False; continue
        if ch == "\\" and in_str:
            esc = True; continue
        if ch == '"':
            in_str = not in_str; continue
        if in_str:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                last_ok = i
    if last_ok > 0:
        try:
            return json.loads(s[:last_ok + 1])
        except json.JSONDecodeError:
            return None
    return None


def _choose_model() -> str:
    """First candidate that hasn't hit 404/quota in this session."""
    global _working_model
    forced = os.environ.get("TRACER_FORCED_MODEL", "").strip()
    if forced and forced not in _failed_models:
        return forced
    if _working_model and _working_model not in _failed_models:
        return _working_model
    for m in MODEL_CANDIDATES:
        if m not in _failed_models:
            return m
    raise RuntimeError(
        "Every Gemini model in MODEL_CANDIDATES failed this session "
        "(quota exhausted or unavailable). Restart to reset, or add a "
        "new model name at the top of agent.py.")


def _is_rotatable_error(exc: Exception) -> bool:
    """Does this error mean 'try a different model'?"""
    text = str(exc)
    name = type(exc).__name__
    if "429" in text or "ResourceExhausted" in name:
        return True
    if "quota" in text.lower() or "per day" in text.lower():
        return True
    if "404" in text and "model" in text.lower():
        return True
    return False


_ASK_SYSTEM = (
    "You are a KiCad and electronics assistant. Answer briefly and directly. "
    "Do NOT emit JSON, circuit specs, or code fences unless the user explicitly asks. "
    "This is Q&A — no artifacts, no injection, just plain-text answers."
)

_PLAN_SYSTEM = (
    "You are a KiCad copilot in PLAN mode. The user is describing a circuit "
    "they want. Your job: outline what you WOULD build, without building it.\n\n"
    "Reply with a short design outline:\n"
    "- **Components** (as a bullet list): each with a reference (R1, U1…), "
    "value or part, and a one-line justification.\n"
    "- **Nets** (bullet list): each with a name and which pins connect.\n"
    "- **Notes** (optional): part-selection tradeoffs, footprints, power "
    "budget, anything the user should decide before you generate.\n\n"
    "Do NOT emit JSON. Do NOT emit KiCad symbol IDs. This is a design "
    "conversation — the user reviews the plan and switches to Agent mode "
    "when ready to actually generate the schematic."
)


def _chat_gemini(
    prompt: str, history: List[Dict[str, str]], system_instruction: str,
    attachments: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Shared plain-text chat path with model rotation. No JSON, no
    clarification handshake — the caller supplies the system prompt that
    shapes the response style (Q&A, plan outline, etc.).
    """
    global _working_model
    genai.configure(api_key=_api_key())
    convo = list(history) + [{"role": "user", "text": prompt}]
    last_error: Optional[Exception] = None
    for _ in range(len(MODEL_CANDIDATES) + 1):
        model_name = _choose_model()
        try:
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=system_instruction,
            )
            resp = model.generate_content(_to_contents(convo, attachments))
            _working_model = model_name
            text = (getattr(resp, "text", None) or "").strip()
            return text or "(no reply)"
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if _is_rotatable_error(exc):
                _failed_models.add(model_name)
                continue
            raise _readable_api_error(exc, model_name) from exc
    raise _readable_api_error(last_error or RuntimeError("all models failed"))


def ask_gemini(prompt: str, history: List[Dict[str, str]],
               attachments: Optional[List[Dict[str, str]]] = None) -> str:
    """Plain Q&A. No circuit output, no clarification handshake, no JSON."""
    return _chat_gemini(prompt, history, _ASK_SYSTEM, attachments)


def plan_gemini(prompt: str, history: List[Dict[str, str]],
                attachments: Optional[List[Dict[str, str]]] = None) -> str:
    """PLAN mode: describe what would be built without building it."""
    return _chat_gemini(prompt, history, _PLAN_SYSTEM, attachments)


def query_gemini(history: List[Dict[str, str]],
                 attachments: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Ask Gemini for a circuit block, or for a clarifying question.

    Takes the whole conversation so far, so an answer to a follow-up is read in
    the context of the original request. Rotates through MODEL_CANDIDATES on
    quota / 404 failures so the whole free tier's daily budget is available.
    """
    global _working_model
    genai.configure(api_key=_api_key())

    last_error: Optional[Exception] = None
    while True:
        model_name = _choose_model()
        model = genai.GenerativeModel(
            model_name=model_name,
            system_instruction=SYSTEM_INSTRUCTION,
            generation_config={
                "response_mime_type": "application/json",
                "temperature": 0.2,
                "max_output_tokens": 8192,
            },
        )
        try:
            response = model.generate_content(_to_contents(history, attachments))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if _is_rotatable_error(exc):
                _failed_models.add(model_name)
                if _working_model == model_name:
                    _working_model = None
                continue  # try next candidate
            raise _readable_api_error(exc, model_name) from exc

        raw = (response.text or "").strip()
        if not raw:
            # Empty response -- treat as rotatable; another model may work.
            _failed_models.add(model_name)
            continue

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # Try a permissive salvage first (trailing commas, unquoted
            # keys, code-fence wrapping). If that also fails, rotate.
            payload = _salvage_json(raw)
            if payload is None:
                _failed_models.add(model_name)
                if _working_model == model_name:
                    _working_model = None
                continue

        _working_model = model_name
        return _unwrap(payload)


def _unwrap(payload: Any) -> Dict[str, Any]:
    """Sort Gemini's answer into one of the two contracted shapes."""
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected a JSON object, got {type(payload).__name__}")

    status = payload.get("status")

    if status == "clarification_needed":
        question = (payload.get("question") or "").strip()
        if not question:
            raise RuntimeError("Gemini asked for clarification but gave no question")
        return {"status": "clarification_needed", "question": question}

    # Anything else is meant to be a circuit. Accept the older bare-block shape
    # and the unwrapped shape too -- both are one bad turn away at temperature.
    circuit = payload.get("circuit")
    if circuit is None:
        circuit = payload.get("block")
    if circuit is None and "components" in payload:
        circuit = {k: v for k, v in payload.items() if k != "status"}
    if not isinstance(circuit, dict):
        raise RuntimeError(
            f"Response carried neither a question nor a circuit (status={status!r}, "
            f"keys={sorted(payload)})"
        )

    return {"status": "success", "circuit": _normalise_circuit(circuit)}


# --- symbol validation -------------------------------------------------------
#
# Gemini invents library and symbol names that don't exist in KiCad. Every
# rejection burns a repair round-trip. Cheaper: check the invented symbol
# against the *actual* installed library table and swap for a generic
# fallback when we can. Uses kicad_sch_api's global symbol cache -- one-time
# library discovery (~2s), then O(1) lookups against a set.

_ALL_SYMBOLS: Optional[set] = None
_SYMBOLS_BY_LIB: Dict[str, List[str]] = {}


def _discover_all_symbols() -> set:
    """Scan every installed .kicad_sym file once, build a flat set of
    Library:SymbolName strings. Direct file scan sidesteps the kicad_sch_api
    cache's lazy-load semantics (which broke search across cold libraries).
    """
    global _ALL_SYMBOLS, _SYMBOLS_BY_LIB
    if _ALL_SYMBOLS is not None:
        return _ALL_SYMBOLS

    import re
    from pathlib import Path

    # KiCad ships symbols under <install>/share/kicad/symbols; the user may
    # also have libs under %APPDATA%/kicad/*.kicad_sym. Discover both.
    search_paths: List[Path] = []
    for env_key in ("KICAD10_SYMBOL_DIR", "KICAD9_SYMBOL_DIR", "KICAD8_SYMBOL_DIR"):
        p = os.environ.get(env_key)
        if p:
            search_paths.append(Path(p))
    # Common Windows install location.
    search_paths.append(Path(r"C:\Program Files\KiCad\10.0\share\kicad\symbols"))
    search_paths.append(Path(r"C:\Program Files\KiCad\9.0\share\kicad\symbols"))
    search_paths.append(Path.home() / "Documents" / "KiCad" / "10.0" / "symbols")
    # De-dupe and keep the first that actually contains .kicad_sym files.
    seen: set = set()
    good: List[Path] = []
    for p in search_paths:
        if p in seen or not p.is_dir():
            continue
        seen.add(p)
        if any(p.glob("*.kicad_sym")):
            good.append(p)

    all_symbols: set = set()
    pat = re.compile(r'\(symbol\s+"([^"]+)"')
    for base in good:
        for lib_file in base.glob("*.kicad_sym"):
            lib_name = lib_file.stem
            try:
                text = lib_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            names: List[str] = []
            for m in pat.finditer(text):
                name = m.group(1)
                # Skip child-part variants (SymbolName_1_1, _0_1) - not directly usable
                if re.search(r"_\d+_\d+$", name):
                    continue
                names.append(name)
                all_symbols.add(f"{lib_name}:{name}")
            _SYMBOLS_BY_LIB[lib_name] = names

    _ALL_SYMBOLS = all_symbols
    return all_symbols


def _symbol_exists(lib_id: str) -> bool:
    """True iff Library:SymbolName resolves to an installed KiCad symbol."""
    syms = _discover_all_symbols()
    if not syms:
        return True  # nothing to validate against -> let the backend decide
    return lib_id in syms


def _search_symbol_by_name(query: str, library: Optional[str] = None) -> Optional[str]:
    """Substring-match against the flat symbol set. Returns the shortest hit
    (usually the base variant, not a suffixed one)."""
    if not query:
        return None
    _discover_all_symbols()
    query_l = query.lower()
    hits: List[str] = []
    if library and library in _SYMBOLS_BY_LIB:
        pool = [(library, n) for n in _SYMBOLS_BY_LIB[library]]
    else:
        pool = [(lib, n) for lib, names in _SYMBOLS_BY_LIB.items() for n in names]
    for lib, name in pool:
        if query_l in name.lower():
            hits.append(f"{lib}:{name}")
    if not hits:
        return None
    return min(hits, key=len)


# ref-prefix -> deterministic fallback. Only used when we're highly confident
# what the user meant (a two-letter passive family), not for ICs.
_REF_FALLBACK = {
    "R": "Device:R",
    "C": "Device:C",
    "L": "Device:L",
    "D": "Device:D",
    "LED": "Device:LED",
    "Q": "Device:Q_NPN_BEC",
    "Y": "Device:Crystal",
    "FB": "Device:Ferrite_Bead",
    "TP": "Connector:TestPoint",
    "SW": "Switch:SW_Push",
}


def _ref_prefix(ref: str) -> str:
    """'R12' -> 'R'; 'LED3' -> 'LED'; 'U1' -> 'U'."""
    i = 0
    while i < len(ref) and not ref[i].isdigit():
        i += 1
    return ref[:i] or ref


def _connector_fallback(ref: str, nets: List[Dict[str, Any]]) -> str:
    """Pick Conn_01xN sized to however many pins nets actually reference."""
    pins = {c["pin"] for net in nets for c in net.get("connections") or []
            if c.get("ref") == ref}
    n = max(2, min(len(pins) or 2, 10))
    return f"Connector_Generic:Conn_01x{n:02d}"


def _ic_fallback(sym: str, value: str) -> Optional[str]:
    """For an IC (U-ref) with a bad symbol, try to find a real match.

    Strategy: peel the suffix off the invented symbol name (`CP2102-GM` ->
    `CP2102`, `ATmega328P-PU` -> `ATmega328P`), search within the claimed
    library first (invented `Interface_USB:CP2102-GM` will match the real
    `Interface_USB:CP2102N-Axx-xQFN28`), then fall back to a global search
    against the base name or the value field.
    """
    import re
    if not sym:
        return None
    lib, _, name = sym.partition(":")
    if not name:
        name = sym
    # Peel dash/underscore suffix down to a stable base name.
    candidates = [name]
    for cut in ("-", "_"):
        stem = name.split(cut, 1)[0]
        if stem and stem != name:
            candidates.append(stem)
    # Also try the value string (often the actual part number).
    if value:
        candidates.append(value)
        for cut in ("-", "_"):
            stem = value.split(cut, 1)[0]
            if stem and stem != value:
                candidates.append(stem)
    # Search claimed library first, then everywhere.
    for query in candidates:
        hit = _search_symbol_by_name(query, library=lib)
        if hit:
            return hit
    for query in candidates:
        hit = _search_symbol_by_name(query)
        if hit:
            return hit
    return None


# Value-authoritative overrides for common ICs Gemini gets structurally wrong.
# When the "value" clearly names one chip family and the symbol names another
# (e.g. value "ESP32-WROOM-32" with symbol "MCU_Espressif:ESP32-C3"), the
# value wins -- Gemini's naming is more reliable than its library recall.
_VALUE_CANONICAL_MAP = [
    # (substring in value, canonical symbol)
    ("ESP32-WROOM-32", "RF_Module:ESP32-WROOM-32"),
    ("ESP32-WROOM-32U", "RF_Module:ESP32-WROOM-32U"),
    ("ESP32-WROOM-32E", "RF_Module:ESP32-WROOM-32E"),
    ("ESP32-S3-WROOM", "RF_Module:ESP32-S3-WROOM-1"),
    ("ESP32-C3-MINI", "RF_Module:ESP32-C3-MINI-1"),
    ("ATmega328P-PU", "MCU_Microchip_ATmega:ATmega328P-PU"),
    ("ATtiny85-20P", "MCU_Microchip_ATtiny:ATtiny85-20P"),
    ("AMS1117-3.3", "Regulator_Linear:AMS1117-3.3"),
    ("AMS1117-5.0", "Regulator_Linear:AMS1117-5.0"),
    ("LM7805", "Regulator_Linear:LM7805_TO220"),
    ("NE555", "Timer:NE555P"),
    ("LM358", "Amplifier_Operational:LM358"),
    ("TL072", "Amplifier_Operational:TL072"),
    ("CP2102N", "Interface_USB:CP2102N-Axx-xQFN28"),
    ("CP2102",  "Interface_USB:CP2102C-Axx-xQFN24"),
    ("CH340C",  "Interface_USB:CH340C"),
    ("FT232RL", "Interface_USB:FT232RL"),
]


def _canonical_for_value(value: str) -> Optional[str]:
    """If `value` names a chip family we have a known-good symbol for,
    return that symbol. Match against the longest key first so
    'ESP32-WROOM-32U' wins over 'ESP32-WROOM-32'."""
    if not value:
        return None
    v_upper = value.upper()
    matches = [(k, v) for k, v in _VALUE_CANONICAL_MAP if k.upper() in v_upper]
    if not matches:
        return None
    # Longest key first so more-specific families win.
    matches.sort(key=lambda kv: -len(kv[0]))
    for _, canonical in matches:
        if _symbol_exists(canonical):
            return canonical
    return None


def _validate_and_swap_symbols(
    circuit: Dict[str, Any], log: Optional[Callable[[str], None]] = None,
) -> None:
    """Replace invented or wrong symbols with real ones in place. Value string
    is authoritative for known IC families; otherwise fall back to ref-prefix
    heuristics or library-index search."""
    log = log or _log_noop
    if not _discover_all_symbols():
        return
    nets = circuit.get("nets") or []
    for comp in circuit.get("components") or []:
        sym = comp.get("symbol", "")
        value = comp.get("value", "")
        # 1. Value wins over symbol for known chip families. Catches cases
        #    like value="ESP32-WROOM-32" with symbol="MCU_Espressif:ESP32-C3".
        canonical = _canonical_for_value(value)
        if canonical and canonical != sym:
            log(f"[preflight] value-swap {comp.get('ref', '?')}: {sym!r} -> {canonical} (value: {value!r})")
            comp["symbol"] = canonical
            continue
        # 2. Everything below only runs if the current symbol is invalid.
        if not sym or _symbol_exists(sym):
            continue
        ref = comp.get("ref", "")
        prefix = _ref_prefix(ref)
        new: Optional[str] = None
        # 1. Passive family fallback: R/C/L/D/LED/Q/Y/FB/TP/SW
        if prefix in _REF_FALLBACK:
            new = _REF_FALLBACK[prefix]
            if prefix == "D" and "LED" in (comp.get("value") or "").upper():
                new = "Device:LED"
        # 2. Connector: pick Conn_01xN sized to net references
        elif prefix == "J":
            new = _connector_fallback(ref, nets)
        # 3. IC: peel-and-search against the actual library table
        else:
            new = _ic_fallback(sym, comp.get("value", ""))
        if new and _symbol_exists(new):
            log(f"[preflight] swapped {ref}: {sym} -> {new}")
            comp["symbol"] = new
        # else: leave it; the LLM repair loop takes over


def _normalise_circuit(
    circuit: Dict[str, Any], log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Nudge the block into the exact shape the backend expects.

    Two things go wrong often enough to be worth fixing here rather than
    spending a round trip on a 422: pin numbers arrive as ints, and optional
    fields arrive as explicit nulls. On top of that, swap any symbol that
    isn't in the installed KiCad library table for a real fallback.
    """
    for net in circuit.get("nets") or []:
        for conn in net.get("connections") or []:
            if isinstance(conn.get("pin"), (int, float)):
                conn["pin"] = str(int(conn["pin"]))

    # The strict fields reject null; dropping the key is what was meant.
    for comp in circuit.get("components") or []:
        for key in ("footprint", "rotation", "role", "value"):
            if comp.get(key) is None:
                comp.pop(key, None)
        # The backend accepts exactly one role value: "controller". Gemini
        # occasionally invents adjacent labels like "connector" or "regulator";
        # drop those rather than 422 the whole build over an optional hint.
        if comp.get("role") not in (None, "controller"):
            comp.pop("role", None)

    _validate_and_swap_symbols(circuit, log)
    return circuit


# --- backend -----------------------------------------------------------------


def _rule(title: str, log: Callable[[str], None]) -> None:
    log(f"\n{title}\n{'-' * max(len(title), 60)}")


def _report(body: Dict[str, Any], log: Callable[[str], None]) -> None:
    """Print a successful generation the way a human wants to read it."""
    _rule("BACKEND RESPONSE", log)
    log(f"  status         : {body.get('status')}")
    log(f"  filename       : {body.get('filename')}")
    log(f"  schematic_path : {body.get('schematic_path')}")
    log(f"  engine_version : {body.get('engine_version')}")

    stats = body.get("stats") or {}
    if stats:
        log("  stats          : " + ", ".join(f"{k}={v}" for k, v in stats.items()))

    erc = body.get("erc") or {}
    _rule("ERC", log)
    if not erc.get("ran", False):
        log("  [--] not run -- kicad-cli was not reachable on this host")
    elif erc.get("clean"):
        log("  [OK] clean -- no violations")
    else:
        violations = erc.get("violations") or []
        log(f"  [!!] {len(violations)} violation(s):")
        for v in violations:
            log(f"    - [{v.get('severity')}] {v.get('type')}: {v.get('description')}")
            for item in v.get("items") or []:
                log(f"        {item}")

    engine_log = body.get("log") or []
    if engine_log:
        _rule("ENGINE LOG", log)
        for line in engine_log:
            log(f"  {line}")


def _report_error(body: Dict[str, Any], log: Callable[[str], None]) -> None:
    """Print the backend's structured rejection."""
    _rule("BACKEND REJECTED THE CIRCUIT", log)
    log(f"  stage  : {body.get('stage')}")
    log(f"  detail : {body.get('detail')}")
    if body.get("hint"):
        log("  hint   : " + "\n           ".join(textwrap.wrap(body["hint"], 66)))
    for err in body.get("errors") or []:
        log(f"    - {err.get('field')}: {err.get('message')}")


def post_to_backend(
    circuit: Dict[str, Any],
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """POST the circuit JSON to the Tracer engine and report what comes back.

    Returns `{"ok": bool, "body": <parsed response>}`; the caller decides what a
    rejection means.
    """
    try:
        response = requests.post(
            BACKEND_URL, json={"block": circuit}, timeout=REQUEST_TIMEOUT_S)
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"Could not reach the backend at {BACKEND_URL}.\n"
            f"Start it first:  uvicorn app:app --reload --port 8000"
        ) from None

    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(
            f"Backend returned HTTP {response.status_code} with a non-JSON body:\n"
            f"{response.text[:1000]}"
        ) from None

    if response.ok:
        _report(body, log)
    else:
        _report_error(body, log)

    return {"ok": response.ok, "body": body}


# --- pipeline ----------------------------------------------------------------


def build_circuit(
    prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    post: bool = True,
    log: Optional[Callable[[str], None]] = print,
    attachments: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """One conversational turn: prompt -> Gemini -> (question | Tracer backend).

    `history` is the conversation so far as `{"role", "text"}` dicts; `prompt`
    is appended to it, so answering a clarification keeps the original request
    in view. Set `post=False` to stop after Gemini and inspect the JSON.

    Returns one of:
        {"status": "clarification_needed", "question": str, "history": [...]}
        {"status": "success", "circuit": {...}, "backend": {...},
         "erc": {...}, "history": [...]}

    The returned `history` already includes this turn, so a caller continuing
    the conversation passes it straight back in.
    """
    log = log or _log_noop
    history = list(history or [])
    history.append({"role": "user", "text": prompt})

    _rule("PROMPT", log)
    for line in textwrap.wrap(prompt, 66):
        log(f"  {line}")

    log(f"\nAsking {_choose_model()} ...")
    result = query_gemini(history, attachments=attachments)
    if _working_model:
        log(f"  (answered by {_working_model})")

    if result["status"] == "clarification_needed":
        question = result["question"]
        # The question goes into history as the model's turn, so the user's
        # answer lands next to it and the original request stays in view.
        history.append({"role": "model", "text": json.dumps(result)})
        _rule("CLARIFICATION NEEDED", log)
        for line in textwrap.wrap(question, 66):
            log(f"  {line}")
        return {
            "status": "clarification_needed",
            "question": question,
            "history": history,
        }

    circuit = result["circuit"]
    history.append({"role": "model", "text": json.dumps({"status": "success",
                                                         "circuit": circuit})})
    log(f"  got '{circuit.get('name', 'unnamed')}': "
        f"{len(circuit.get('components') or [])} components, "
        f"{len(circuit.get('nets') or [])} nets")

    _rule("GENERATED CIRCUIT JSON", log)
    log(textwrap.indent(json.dumps(circuit, indent=2), "  "))

    out: Dict[str, Any] = {
        "status": "success",
        "circuit": circuit,
        "history": history,
    }

    if post:
        log(f"\nPOSTing to {BACKEND_URL} ...")
        posted = post_to_backend(circuit, log)
        out["backend"] = posted["body"]
        out["erc"] = posted["body"].get("erc") or {}
        if not posted["ok"]:
            out["status"] = "backend_rejected"
            body = posted["body"]
            out["error_detail"] = body.get("detail", "")
            out["error_stage"] = body.get("stage", "")

    return out


_REPAIR_GUIDANCE = {
    "symbol-lookup": """\
The named symbol is NOT in the installed KiCad libraries. Return a corrected
circuit that:
  1. Replaces the offending symbol with a real, standard one. When unsure,
     fall back to the generic Device: or Connector_Generic: equivalent
     (Device:R, Device:C, Device:LED, Device:D, Connector_Generic:Conn_01x02,
     Connector_Generic:Conn_01x04) and put the specific part number in the
     "value" field.
  2. For USB-C receptacles use 'Connector:USB_C_Receptacle_USB2.0_16P' (16-pin
     USB 2.0 flavour) or the generic 'Connector_Generic:Conn_01x06'. Do NOT
     invent 'USB_C_Reversible'.
  3. For serial bridges the real symbol is 'Interface_USB:CP2102N-Axx-xQFN28'
     (lowercase 'x' in the suffix -- KiCad's placeholder for any variant).
     Alternatives: 'Interface_USB:CP2102C-Axx-xQFN24', 'Interface_USB:CP2104',
     'Interface_USB:CH340C', 'Interface_USB:FT232RL'. If unsure, fall back
     to a Connector_Generic 4-pin header and note the chip in "value".
  4. Do NOT invent library or symbol names. Keep every other component and
     net unchanged.""",

    "pin-lookup": """\
A net references a pin that does not exist on the named symbol. Return a
corrected circuit that:
  1. Removes the invented pin connection or moves it to a real pin number
     of that symbol -- pin numbers are the part's own, not sequential.
  2. If the symbol is a large IC and you are unsure of the pinout, drop the
     connection entirely rather than guess. A missing net is better than a
     wrong one.
  3. Keep every other component and net unchanged.""",

    "request-validation": """\
The JSON did not match the strict backend schema. Return a corrected circuit
that:
  1. Removes any field the schema does not allow. The only valid role value
     is "controller"; do NOT invent "connector", "regulator", "sensor" etc.
  2. Uses the exact key names and value types from the system instructions.
  3. Keeps every component and net you had, minus the invalid fields.""",

    "verification-routing": """\
The layout engine could not find a clear routing path. Return a corrected
circuit that:
  1. Raises layout.channel_mm and layout.body_margin_mm (try 20.32 and 5.08,
     then 25.4 and 7.62);
  2. Moves the blocking part to a different row, or spreads that column's
     parts over more rows;
  3. Rotates a part so its pins face the signal flow;
  4. If the failing net names a two-pin part, checks its polarity -- on
     Device:LED and Device:D pin 1 is the cathode and pin 2 the anode.""",

    "verification-offsheet": """\
Components or labels ran OFF the sheet's edge. The block is bigger than the
sheet, so making it roomier will only make it worse. Return a corrected
circuit that:
  1. Escalates the sheet size: change "sheet" from "A4" (297x210 mm) to "A3"
     (420x297 mm), or from "A3" to "A2" (594x420 mm). Use "A2" for anything
     over ~12 components with several columns of decoupling caps.
  2. LOWERS layout.channel_mm and layout.body_margin_mm back to defaults
     (channel_mm 15.24, body_margin_mm 3.81) -- overspacing was making the
     block spill off the sheet. Do NOT raise them.
  3. Keeps every component and net you had; only "sheet" and layout
     spacing may change.""",

    "layout": """\
The layout engine rejected the geometry. Raise layout.channel_mm and
layout.body_margin_mm modestly, spread crowded columns, rotate parts to
align with signal flow, and check two-pin polarity.""",
}


def _classify_verification(detail: str) -> str:
    """Look at the engine's verification detail; pick the right recipe."""
    low = (detail or "").lower()
    if "off the" in low and "sheet" in low:
        return "verification-offsheet"
    return "verification-routing"


def repair_prompt(result: Dict[str, Any]) -> str:
    """Turn an engine rejection into a stage-aware instruction Gemini can act on.

    Each backend failure stage (symbol-lookup, pin-lookup, request-validation,
    verification, layout) has its own concrete repair recipe -- a generic
    "fix it" prompt burns a turn on a near-identical retry, whereas a
    stage-specific one names the exact knob to change. Verification splits
    further: "no clear path" wants more spacing, "off the sheet" wants a
    bigger sheet and less spacing -- opposite fixes for opposite failures.
    """
    stage = result.get("error_stage") or "verification"
    detail = result.get("error_detail") or "no detail given"

    guidance_key = stage
    if stage == "verification":
        guidance_key = _classify_verification(detail)

    guidance = _REPAIR_GUIDANCE.get(guidance_key, _REPAIR_GUIDANCE["layout"])
    return (
        f"The backend rejected that circuit at the '{stage}' stage:\n\n"
        f"{detail}\n\n"
        f"{guidance}\n\n"
        f"Do not ask a question; return a full \"success\" payload with the fix."
    )


_SHEET_ESCALATION = {"A4": "A3", "A3": "A2", "A": "A2", "B": "A2"}
_DEFAULT_LAYOUT_RESET = {
    "channel_mm": 15.24,
    "body_margin_mm": 3.81,
    "rail_drop_mm": 12.7,
}


def try_mechanical_repair(
    circuit: Dict[str, Any], result: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Repair a rejection deterministically, without another Gemini call.

    Some failure modes have a mechanical fix that doesn't need any judgement --
    e.g. an off-the-sheet failure just needs a bigger sheet size. Doing that
    in Python is faster, cheaper, and more reliable than asking Gemini to
    re-emit the whole circuit.

    Returns a patched circuit dict on success, or None if no local fix
    applies and we should fall through to the LLM-based repair.
    """
    stage = result.get("error_stage") or ""
    detail = (result.get("error_detail") or "").lower()

    if stage == "verification" and "off the" in detail and "sheet" in detail:
        current = circuit.get("sheet", "A4")
        nxt = _SHEET_ESCALATION.get(current)
        if nxt and nxt != current:
            patched = json.loads(json.dumps(circuit))  # deep copy
            patched["sheet"] = nxt
            # Reset spacing -- a previous retry may have raised it, which
            # only made the off-sheet problem worse.
            patched.setdefault("layout", {})
            patched["layout"].update(_DEFAULT_LAYOUT_RESET)
            return patched

    return None


def build_with_auto_repair(
    prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    log: Optional[Callable[[str], None]] = None,
    max_repairs: int = MAX_REPAIRS,
    attachments: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Like build_circuit, but retries on backend_rejected using repair_prompt.

    Clarification requests propagate up unchanged (the caller answers them).
    A backend rejection is fed back to Gemini as a stage-specific repair
    instruction, up to `max_repairs` times before giving up.
    """
    log = log or _log_noop
    history = list(history or [])
    turn = prompt
    result: Dict[str, Any] = {}
    for attempt in range(max_repairs + 1):
        # Attachments only ride the first turn — repair turns are pure
        # text prompts about backend errors.
        first_turn_atts = attachments if attempt == 0 else None
        result = build_circuit(turn, history, post=True, log=log,
                               attachments=first_turn_atts)
        history = result.get("history", history)

        if result.get("status") != "backend_rejected":
            return result

        # Try to fix it locally without burning a Gemini turn. Loops until
        # the mechanical fix stops making progress (or the backend accepts).
        circuit = result.get("circuit")
        mech_rounds = 0
        while circuit is not None and mech_rounds < 3:
            patched = try_mechanical_repair(circuit, result)
            if patched is None:
                break
            mech_rounds += 1
            log(f"\n[auto-repair] mechanical fix: sheet -> "
                f"{patched.get('sheet')}, layout reset.")
            posted = post_to_backend(patched, log=log)
            if posted["ok"]:
                # Success path -- backend accepted the patched circuit.
                history.append({"role": "model",
                                "text": json.dumps({"status": "success",
                                                    "circuit": patched})})
                return {
                    "status": "success",
                    "circuit": patched,
                    "backend": posted["body"],
                    "erc": posted["body"].get("erc") or {},
                    "history": history,
                    "_repair_count": attempt,
                    "_mechanical_repairs": mech_rounds,
                }
            # backend still rejects -- update result and either loop again
            # (if next mechanical step exists) or fall through to Gemini.
            body = posted["body"]
            result = {
                "status": "backend_rejected",
                "circuit": patched,
                "backend": body,
                "error_stage": body.get("stage", ""),
                "error_detail": body.get("detail", ""),
                "history": history,
                "_repair_count": attempt,
            }
            circuit = patched

        if attempt >= max_repairs:
            log(f"\n[auto-repair] gave up after {attempt + 1} attempt(s); "
                f"last stage was '{result.get('error_stage', '?')}'.")
            return result

        log(f"\n[auto-repair] backend rejected at "
            f"'{result.get('error_stage', '?')}' -- asking Gemini to fix "
            f"(attempt {attempt + 2}/{max_repairs + 1}).")
        turn = repair_prompt(result)
        result["_repair_count"] = attempt + 1
    return result


def run_conversation(
    prompt: str,
    answer: Callable[[str], Optional[str]],
    log: Optional[Callable[[str], None]] = print,
) -> Dict[str, Any]:
    """Drive `build_circuit` until it stops asking questions.

    `answer` is handed each clarifying question and returns the user's reply, or
    None to give up. The KiCad plugin does not use this -- it runs the same loop
    itself so the question can be answered in a wx dialog.
    """
    history: List[Dict[str, str]] = []
    turn = prompt
    repairs_left = MAX_REPAIRS
    for _ in range(MAX_CLARIFICATIONS + MAX_REPAIRS + 1):
        result = build_circuit(turn, history, log=log)
        history = result.get("history", history)

        if result["status"] == "clarification_needed":
            reply = answer(result["question"])
            if not reply:
                return {"status": "cancelled", "history": history}
            turn = reply
            continue

        if result["status"] == "backend_rejected" and repairs_left:
            repairs_left -= 1
            log = log or _log_noop
            log(f"\n[repair] asking for a layout fix "
                f"({MAX_REPAIRS - repairs_left}/{MAX_REPAIRS})")
            turn = repair_prompt(result)
            continue

        return result
    raise RuntimeError("Too many rounds without a buildable circuit; giving up.")


# --- JSON mode ---------------------------------------------------------------


def _serve_json() -> int:
    """One JSON request in on stdin, one JSON response out on stdout.

    This is the plugin's transport. KiCad's bundled Python has no Gemini SDK,
    so the plugin shells out to this interpreter instead; keeping it to a
    single request per process means no protocol state to get wrong.

    Request:   {"prompt": str, "history": [...], "post": bool}
    Response:  whatever build_circuit returns, or {"status": "error", ...}
    """
    # Human-readable logging must not pollute the JSON on stdout.
    def log(line: str) -> None:
        print(line, file=sys.stderr)

    try:
        request = json.loads(sys.stdin.read() or "{}")
        prompt = (request.get("prompt") or "").strip()
        if not prompt:
            raise RuntimeError("Request carried no 'prompt'")
        result = build_circuit(
            prompt,
            history=request.get("history") or [],
            post=bool(request.get("post", True)),
            log=log,
        )
    except Exception as exc:  # noqa: BLE001 -- the caller wants every failure as JSON
        # RuntimeError is what this module raises for messages already written
        # to be read by a human, so it carries no type prefix; anything else is
        # unexpected and the class name is worth having.
        message = str(exc) if isinstance(exc, RuntimeError) \
            else f"{type(exc).__name__}: {exc}"
        result = {"status": "error", "error": message}

    json.dump(result, sys.stdout)
    sys.stdout.flush()
    return 0 if result.get("status") in ("success", "clarification_needed") else 1


SAMPLE_PROMPT = (
    "Build an ATtiny85 board powered from a 2-pin 5V header through an "
    "AMS1117-3.3 regulator. Decouple the regulator input and output, decouple "
    "the MCU supply, add a 10k pull-up on RESET, and drive two indicator LEDs "
    "from PB0 and PB1 through 330 ohm series resistors."
)


def _ask_at_console(question: str) -> Optional[str]:
    print(f"\n  ? {question}")
    try:
        return input("  > ").strip() or None
    except (EOFError, KeyboardInterrupt):
        return None


def _check_key() -> int:
    """Report where the key came from and whether Gemini actually accepts it.

    Onboarding a tester is where key setup goes wrong, and the two failure
    modes look identical from the outside: a key that was never picked up, and
    a key that was picked up but is rejected. So this says which file or
    variable the key came from, then spends one tiny request proving it works.
    """
    env_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    dotenv_path = HERE / ".env"

    if env_key:
        source = "the GEMINI_API_KEY environment variable"
    elif _load_dotenv().get("GEMINI_API_KEY"):
        source = str(dotenv_path)
    else:
        source = None

    try:
        key = _api_key()
    except RuntimeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr, flush=True)
        if dotenv_path.is_file():
            print(f"\n{dotenv_path} exists but carries no usable key.",
                  file=sys.stderr, flush=True)
        else:
            print(f"\nThere is no {dotenv_path} yet. Copy .env.example to "
                  f".env and paste your key into it.", file=sys.stderr, flush=True)
        return 1

    print(f"[ok]   key found in {source}", flush=True)
    # Only ever show the ends: enough to tell two keys apart, never enough to
    # reuse if this output gets pasted into an issue.
    print(f"       {key[:6]}...{key[-4:]}  ({len(key)} characters)", flush=True)

    if env_key and _load_dotenv().get("GEMINI_API_KEY"):
        print("[note] .env also sets GEMINI_API_KEY; the environment variable "
              "wins.")

    print(f"[..]   asking Gemini to confirm it ({MODEL_NAME})...", flush=True)
    try:
        genai.configure(api_key=key)
        genai.GenerativeModel(MODEL_NAME).generate_content(
            "Reply with the single word: ok")
    except Exception as exc:  # noqa: BLE001 -- every failure is reported the same way
        print(f"[FAIL] {_readable_api_error(exc)}", file=sys.stderr, flush=True)
        return 1

    print("[ok]   Gemini accepted the key. You are ready to go.", flush=True)
    return 0


if __name__ == "__main__":
    if "--check-key" in sys.argv[1:]:
        raise SystemExit(_check_key())

    if "--json" in sys.argv[1:]:
        raise SystemExit(_serve_json())

    # Anything on the command line wins; otherwise run the sample prompt.
    user_prompt = " ".join(sys.argv[1:]).strip() or SAMPLE_PROMPT
    try:
        outcome = run_conversation(user_prompt, _ask_at_console)
        if outcome["status"] == "cancelled":
            print("\n[cancelled] no answer given.")
            raise SystemExit(1)
        raise SystemExit(0 if outcome["status"] == "success" else 1)
    except RuntimeError as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
