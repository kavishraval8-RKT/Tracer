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
MODEL_NAME = "gemini-3.5-flash"
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


def _to_contents(history: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Turn portable history into the SDK's `contents` list."""
    contents = []
    for turn in history:
        role = "model" if turn.get("role") == "model" else "user"
        contents.append({"role": role, "parts": [{"text": turn.get("text", "")}]})
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


def query_gemini(history: List[Dict[str, str]]) -> Dict[str, Any]:
    """Ask Gemini for a circuit block, or for a clarifying question.

    Takes the whole conversation so far, so an answer to a follow-up is read in
    the context of the original request.
    """
    genai.configure(api_key=_api_key())
    model = genai.GenerativeModel(
        model_name=MODEL_NAME,
        system_instruction=SYSTEM_INSTRUCTION,
        generation_config={
            # The whole point of this call: JSON out, never prose.
            "response_mime_type": "application/json",
            "temperature": 0.2,
            "max_output_tokens": 8192,
        },
    )

    try:
        response = model.generate_content(_to_contents(history))
    except Exception as exc:  # noqa: BLE001 -- re-raised, just more legibly
        raise _readable_api_error(exc, MODEL_NAME) from exc

    raw = (response.text or "").strip()
    if not raw:
        finish = "unknown"
        if response.candidates:
            finish = getattr(response.candidates[0], "finish_reason", "unknown")
        raise RuntimeError(f"Gemini returned nothing. Finish reason: {finish}")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Gemini did not return valid JSON ({exc}).\n"
            f"--- raw response ---\n{raw[:2000]}"
        ) from exc

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


def _normalise_circuit(circuit: Dict[str, Any]) -> Dict[str, Any]:
    """Nudge the block into the exact shape the backend expects.

    Two things go wrong often enough to be worth fixing here rather than
    spending a round trip on a 422: pin numbers arrive as ints, and optional
    fields arrive as explicit nulls.
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

    log(f"\nAsking {MODEL_NAME} ...")
    result = query_gemini(history)

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


def repair_prompt(result: Dict[str, Any]) -> str:
    """Turn an engine rejection into an instruction Gemini can act on.

    The layout engine's complaints are specific and mechanical -- which net
    could not be routed, which pin was in the way -- so handing the text back
    verbatim is more useful than paraphrasing it.

    The list is deliberately concrete. A bare "give the parts more room" let the
    model resend a near-identical layout, so the repair round burned a turn and
    failed on the same obstacle; naming the two spacing knobs and the polarity
    trap gives it something it can actually change.
    """
    template = """\
The layout engine rejected that circuit at the '{stage}' stage:

{detail}

Return the corrected circuit as a full "success" payload. Keep the same
components and the same nets -- change only the layout. Do not resend the
layout you just sent; change at least two of these:
  1. raise layout.channel_mm and layout.body_margin_mm (try 20.32 and 5.08,
     then 25.4 and 7.62);
  2. move the blocking part to a different row, or spread that column's parts
     over more rows;
  3. rotate a part so its pins face the signal flow;
  4. if the failing net names a two-pin part, check its polarity -- on
     Device:LED and Device:D pin 1 is the cathode and pin 2 the anode, and a
     net on the wrong pin makes the part face backwards.
Do not ask a question; fix the geometry."""
    return template.format(
        stage=result.get("error_stage") or "verification",
        detail=result.get("error_detail") or "no detail given")


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
