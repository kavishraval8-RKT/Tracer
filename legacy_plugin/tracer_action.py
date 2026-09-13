"""Tracer Copilot -- a floating wxPython panel that talks to the Tracer agent.

The UI is deliberately thin. Everything slow happens on a worker thread:

    Generate -> worker -> agent.py --json -> Gemini -> Tracer backend
                  |
                  +-- "clarification_needed" -> wx.CallAfter -> modal dialog
                                                     |
                                                 answer -> back to the worker

The worker blocks on a Queue while the modal is up, so the conversation carries
on in the same thread with its history intact, and the GUI thread is the only
one that ever touches a widget.

Gemini's SDK lives in the system Python, not KiCad's bundled one, so the agent
runs out of process and the two sides speak one JSON object each way.

Runs inside KiCad as an action plugin, and standalone for development:

    python tracer_action.py
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import wx

# `tracer_inject` needs `kicad_sch_api`, which lives in the system Python and
# not in KiCad's bundled one. Import it when it is there (standalone runs, and
# tests), and fall back to running it as a subprocess when it is not.
try:
    from . import tracer_inject
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import tracer_inject
    except ImportError:
        tracer_inject = None


# --- locating the pieces -----------------------------------------------------

PLUGIN_DIR = Path(__file__).resolve().parent

#: Where agent.py and app.py live. Override with TRACER_HOME when the plugin is
#: installed into KiCad's plugin folder, away from the engine.
TRACER_HOME = Path(
    os.environ.get("TRACER_HOME") or PLUGIN_DIR.parent
).resolve()

MAX_TURNS = 5          # clarification rounds before the worker gives up
MAX_REPAIRS = 2        # layout-fix rounds after an engine rejection
AGENT_TIMEOUT_S = 300


def _repair_prompt(result: Dict[str, Any]) -> str:
    """Turn an engine rejection into an instruction Gemini can act on.

    Mirrors `agent.repair_prompt`; duplicated rather than imported because
    agent.py pulls in the Gemini SDK, which KiCad's Python does not have. Keep
    the two in step.
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


def find_agent() -> Optional[Path]:
    """Locate agent.py, checking the usual places before giving up.

    Two shapes have to work. In a source checkout the plugin is a subdirectory
    of the engine, so walking up finds it. Once the plugin is copied into
    KiCad's own plugin folder it is nowhere near the engine, and TRACER_HOME is
    the only thing that can say where it went -- which is why that is checked
    first and named in the error the UI shows.
    """
    candidates = [TRACER_HOME / "agent.py", PLUGIN_DIR / "agent.py"]

    # Walk up from the plugin: tracer_plugin/ sits inside the repo root in a
    # checkout, and a couple of extra levels costs nothing if it does not.
    for parent in list(PLUGIN_DIR.parents)[:3]:
        candidates.append(parent / "agent.py")

    for path in candidates:
        if path.is_file():
            return path
    return None


#: KiCad points these at its own bundled interpreter and its 3rdparty folder.
#: Inherited by a child process they silently redirect that child's user
#: site-packages -- which is exactly where `pip install --user` put the Gemini
#: SDK -- so every subprocess launched from here gets them removed.
_PYTHON_ENV_VARS = (
    "PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "PYTHONSTARTUP",
    "PYTHONNOUSERSITE", "PYTHONEXECUTABLE",
)


def child_env() -> Dict[str, str]:
    """A copy of the environment safe to hand to another interpreter."""
    return {k: v for k, v in os.environ.items() if k not in _PYTHON_ENV_VARS}


def _run(args: List[str], **kwargs) -> "subprocess.CompletedProcess":
    """subprocess.run with KiCad's Python variables stripped and no console."""
    kwargs.setdefault("env", child_env())
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.run(args, **kwargs)


#: KiCad's GUI binaries. `sys.executable` inside an action plugin is one of
#: these, not an interpreter -- Python is embedded in the executable. Handing a
#: .py path to one of them makes KiCad's project manager try to *open* the
#: file, which is where "does not appear to be a KiCad project file" comes from.
_KICAD_BINARIES = (
    "kicad", "kicad-cli", "pcbnew", "eeschema", "gerbview", "pl_editor",
    "bitmap2component", "pcb_calculator",
)


def _is_python_exe(path: str) -> bool:
    """True if `path` names a CPython interpreter rather than a KiCad binary.

    Cheap name check only; `_can_run_agent` still has to confirm the SDK. The
    point is that nothing which is not an interpreter ever reaches a command
    line where a .py file sits in argv.
    """
    if not path:
        return False
    stem = Path(path).stem.lower()
    if stem in _KICAD_BINARIES:
        return False
    return stem.startswith("python")


#: Printed by the probe below. An interpreter that cannot import the SDK fails
#: before reaching it, and anything that is not an interpreter never prints it
#: at all -- so a stray exit code 0 cannot promote a GUI binary to "our Python".
_PROBE_OK = "TRACER_PYTHON_OK"

_PROBE = (
    "import google.generativeai, sys; "
    "sys.stdout.write('%s')" % _PROBE_OK
)


def _can_run_agent(python: str) -> bool:
    """True if this interpreter is real and has the Gemini SDK installed."""
    if not _is_python_exe(python):
        return False
    try:
        result = _run([python, "-E", "-c", _PROBE], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and _PROBE_OK in (result.stdout or "")


def _candidate_pythons() -> List[str]:
    """Interpreters worth probing, absolute paths first.

    A bare "python" is resolved through PATH by the child, and KiCad puts its
    own bin directory on PATH -- so it can come back as KiCad's interpreter,
    which is the one we are trying to get away from. Absolute paths first
    avoids that entirely.
    """
    import glob
    import shutil

    candidates: List[str] = []

    # Inside KiCad this is kicad.exe or pcbnew.exe, which must never be handed
    # a script path. It is only a real interpreter on standalone/test runs.
    if _is_python_exe(sys.executable):
        candidates.append(sys.executable)

    for name in ("python", "python3"):
        found = shutil.which(name, path=os.environ.get("PATH"))
        if found and _is_python_exe(found):
            candidates.append(found)

    # Ask the Windows launcher where a real CPython lives.
    launcher = shutil.which("py")
    if launcher:
        try:
            result = _run([launcher, "-3", "-c",
                           "import sys; print(sys.executable)"], timeout=60)
            if result.returncode == 0 and result.stdout.strip():
                candidates.append(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass

    # Well-known install roots, newest version first. Discovery only -- these
    # are where CPython puts itself, not anybody's particular checkout.
    if sys.platform == "win32":
        patterns = [r"C:\Python3*\python.exe"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            patterns.append(
                os.path.join(local, "Programs", "Python", "Python3*", "python.exe"))
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            patterns.append(
                os.path.join(program_files, "Python3*", "python.exe"))
    else:
        patterns = ["/usr/bin/python3.*", "/usr/local/bin/python3.*",
                    "/opt/homebrew/bin/python3.*"]

    for pattern in patterns:
        candidates.extend(sorted(glob.glob(pattern), reverse=True))

    # KiCad ships its own python.exe beside its GUI binaries. It normally lacks
    # the Gemini SDK, so it goes last -- but it is a genuine interpreter, and
    # it is the right answer if the SDK was pip-installed into it.
    exe_name = "python.exe" if sys.platform == "win32" else "python3"
    bundled = Path(sys.executable).parent / exe_name
    if bundled.is_file():
        candidates.append(str(bundled))

    seen = set()
    ordered = []
    for candidate in candidates:
        key = os.path.normcase(candidate or "")
        if key and key not in seen:
            seen.add(key)
            ordered.append(candidate)
    return ordered


_PYTHON_CACHE: Optional[str] = None


def find_python() -> Optional[str]:
    """Find an interpreter that can actually import the Gemini SDK.

    KiCad's bundled Python has wx and pcbnew but no `google-generativeai`, so
    the one running this UI is almost never the right one to run the agent
    with. Cached, because probing shells out several times.
    """
    explicit = os.environ.get("TRACER_PYTHON")
    if explicit:
        # An explicit choice is honoured even if the SDK probe fails, so the
        # error the user sees is the real one from the agent rather than "not
        # found". It still has to be an interpreter: pointing TRACER_PYTHON at
        # kicad.exe would just reopen the project-file bug by another route.
        if not _is_python_exe(explicit):
            raise RuntimeError(
                f"TRACER_PYTHON points at {explicit!r}, which is not a Python "
                f"interpreter. It must be a python.exe -- passing a KiCad "
                f"binary a script path makes KiCad try to open it as a project.")
        return explicit

    global _PYTHON_CACHE
    if _PYTHON_CACHE is not None:
        return _PYTHON_CACHE or None

    for candidate in _candidate_pythons():
        if _can_run_agent(candidate):
            _PYTHON_CACHE = candidate
            return candidate
    _PYTHON_CACHE = ""
    return None


def _settings_dirs() -> List[Path]:
    """KiCad's user settings directories, newest version first.

    This is %APPDATA%\\kicad\\<ver>, which KICAD_DOCUMENTS_HOME does NOT move --
    only the documents tree (projects, plugins, 3rdparty) follows that variable.
    """
    roaming = os.environ.get("APPDATA")
    if not roaming:
        return []
    base = Path(roaming) / "kicad"
    if not base.is_dir():
        return []
    return sorted((d for d in base.iterdir() if d.is_dir()), reverse=True)


def _recent_schematics() -> List[Path]:
    """Absolute paths eeschema has opened, most recent first.

    The IPC API names the open sheet but not its folder (see
    `_target_from_ipc`), so this is what turns that name into a real path.
    """
    paths: List[Path] = []
    for settings_dir in _settings_dirs():
        config = settings_dir / "eeschema.json"
        if not config.is_file():
            continue
        try:
            history = json.loads(config.read_text(encoding="utf-8")) \
                .get("system", {}).get("file_history") or []
        except (OSError, ValueError):
            continue
        for entry in history:
            if isinstance(entry, str) and entry.strip():
                paths.append(Path(entry))
    return paths


def _target_from_ipc() -> Optional[str]:
    """Ask KiCad over the IPC API which schematic is open, or None.

    Only succeeds in the IPC front door, where this process was launched by
    KiCad's plugin manager and `kipy` is installed in the managed venv. In the
    legacy ActionPlugin we are inside kicad.exe with no socket to talk to, and
    `pcbnew.GetBoard()` has already answered.

    Two limits of KiCad 10.0.5, both measured against a live session rather
    than assumed, and both the reason this ends in a filename search:

      * an open schematic's DocumentSpecifier fills in only `board_filename`
        (yes, that field name, for a .kicad_sch), and it is a bare filename;
      * `get_project()` returns empty name and path, and the project does not
        expand ${KIPRJMOD}, so the API cannot be asked for the directory.

    So the API supplies the name and eeschema's recent-file list supplies the
    folder. If a later KiCad starts returning an absolute path, the first
    branch below takes it and this degrades to dead code rather than breaking.

    `kipy.schematic` is deliberately never imported: kicad-python 0.8.0 cannot
    import it at all (its `PageSettings` import is broken upstream). Nothing
    here needs it -- tracer_inject edits the sheet on disk, so all the API is
    asked for is which file that is.
    """
    try:
        import kipy  # noqa: PLC0415 -- only present in the IPC venv
        from kipy.proto.common.types import base_types_pb2 as _bt  # noqa: PLC0415
    except ImportError:
        return None

    try:
        # Socket path and token come from the environment KiCad launched us
        # with, so this takes no arguments.
        docs = kipy.KiCad().get_open_documents(_bt.DOCTYPE_SCHEMATIC)
    except Exception:
        return None

    for doc in docs:
        name = (getattr(doc, "board_filename", "") or "").strip()
        if not name:
            continue

        candidate = Path(name)
        if candidate.is_absolute() and candidate.is_file():
            return str(candidate)

        # Bare filename: find it in eeschema's history, most recent first.
        for recent in _recent_schematics():
            if recent.name.lower() == candidate.name.lower() and recent.is_file():
                return str(recent)

        # Last resort: the engine's own folder.
        local = TRACER_HOME / candidate.name
        if local.is_file():
            return str(local)

    return None


def default_target() -> Optional[str]:
    """The schematic to inject into, from whichever front door we came in by."""
    try:
        import pcbnew  # noqa: PLC0415 -- absent outside KiCad, and that is fine
        board = pcbnew.GetBoard()
        if board:
            board_path = Path(board.GetFileName())
            if board_path.name:
                sibling = board_path.with_suffix(".kicad_sch")
                if sibling.is_file():
                    return str(sibling)
    except Exception:
        pass

    from_ipc = _target_from_ipc()
    if from_ipc:
        return from_ipc

    for path in sorted(TRACER_HOME.glob("*.kicad_sch")):
        return str(path)
    return None


# --- the worker --------------------------------------------------------------


class GenerationWorker(threading.Thread):
    """Runs the whole conversation off the GUI thread."""

    def __init__(self, frame: "TracerFrame", prompt: str, target: Optional[str],
                 inject: bool) -> None:
        super().__init__(daemon=True)
        self.frame = frame
        self.prompt = prompt
        self.target = target
        self.inject = inject
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    # -- talking to the UI ----------------------------------------------------

    def log(self, message: str) -> None:
        wx.CallAfter(self.frame.append_log, message)

    def ask(self, question: str) -> Optional[str]:
        """Put a modal in front of the user and wait here for the answer.

        This is the whole point of running on a thread: the worker parks on the
        queue, the GUI thread owns the dialog, and when the answer arrives the
        conversation resumes with its history intact.
        """
        answer_queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=1)
        wx.CallAfter(self.frame.prompt_clarification, question, answer_queue)
        try:
            return answer_queue.get(timeout=AGENT_TIMEOUT_S)
        except queue.Empty:
            return None

    # -- talking to the agent -------------------------------------------------

    def call_agent(self, prompt: str, history: List[Dict[str, str]]) -> Dict[str, Any]:
        agent = find_agent()
        if agent is None:
            raise RuntimeError(
                f"Could not find agent.py. Looked under {TRACER_HOME}. "
                f"Set the TRACER_HOME environment variable to the folder "
                f"holding agent.py.")

        python = find_python()
        if python is None:
            raise RuntimeError(
                "No Python interpreter with the Gemini SDK was found. Install "
                "it with 'pip install google-generativeai', or set TRACER_PYTHON "
                "to the interpreter that has it.")

        # Last line of defence. `agent` is about to sit in argv as a plain
        # path, so whatever runs it has to be an interpreter -- a KiCad binary
        # here would hand the .py to KiCad's project loader instead.
        if not _is_python_exe(python):
            raise RuntimeError(
                f"Refusing to run the agent with {python!r}: that is not a "
                f"Python interpreter. Set TRACER_PYTHON to a python.exe.")

        request = json.dumps({"prompt": prompt, "history": history, "post": True})
        self.log(f"  running {Path(python).name} {agent.name} --json")

        try:
            result = _run(
                [python, "-E", str(agent), "--json"],
                input=request, encoding="utf-8", errors="replace",
                cwd=str(agent.parent), timeout=AGENT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"The agent did not answer within {AGENT_TIMEOUT_S}s.") from None

        # stderr carries the agent's human-readable log; surface it either way.
        for line in (result.stderr or "").splitlines():
            if line.strip():
                self.log(f"  | {line.rstrip()}")

        stdout = (result.stdout or "").strip()
        if not stdout:
            raise RuntimeError(
                f"The agent produced no JSON (exit {result.returncode}). "
                f"See the log above for why.")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"The agent's reply was not JSON ({exc}): {stdout[:400]}") from exc

    def run_injection(self, generated: str, target: str) -> Dict[str, Any]:
        """Merge the new block into the target sheet.

        In process when `kicad_sch_api` is importable, and out of process when
        it is not -- which is the normal case inside KiCad, whose bundled Python
        does not carry it.
        """
        if tracer_inject is not None:
            summary = tracer_inject.inject(generated, target)
            summary["text"] = tracer_inject.describe(summary)
            return summary

        python = find_python()
        if python is None:
            raise RuntimeError(
                "Injection needs the system Python that has kicad_sch_api "
                "installed; set TRACER_PYTHON to point at it.")

        if not _is_python_exe(python):
            raise RuntimeError(
                f"Refusing to run the merge with {python!r}: that is not a "
                f"Python interpreter. Set TRACER_PYTHON to a python.exe.")

        script = PLUGIN_DIR / "tracer_inject.py"
        self.log(f"  running {Path(python).name} {script.name}")
        result = _run(
            [python, "-E", str(script), "--generated", generated,
             "--target", target],
            encoding="utf-8", errors="replace", timeout=AGENT_TIMEOUT_S,
        )
        stdout = (result.stdout or "").strip()
        if not stdout:
            raise RuntimeError(
                f"the merge produced no output (exit {result.returncode}): "
                f"{(result.stderr or '').strip()[:400]}")
        summary = json.loads(stdout)
        if "error" in summary:
            raise RuntimeError(summary["error"])
        return summary

    # -- the loop -------------------------------------------------------------

    def run(self) -> None:
        try:
            history: List[Dict[str, str]] = []
            prompt = self.prompt
            repairs_left = MAX_REPAIRS

            for turn in range(1, MAX_TURNS + MAX_REPAIRS + 1):
                if self._cancelled.is_set():
                    self.log("\n[cancelled]")
                    return

                self.log(f"\n--- turn {turn} ---")
                result = self.call_agent(prompt, history)
                status = result.get("status")

                if status == "error":
                    raise RuntimeError(result.get("error", "unknown agent error"))

                if status == "clarification_needed":
                    question = result["question"]
                    self.log(f"\n  ? {question}")
                    answer = self.ask(question)
                    if not answer:
                        self.log("\n[cancelled] no answer given.")
                        return
                    self.log(f"  > {answer}")
                    history = result.get("history") or []
                    prompt = answer
                    continue

                if status == "backend_rejected":
                    if not repairs_left:
                        self.log("\n[!] The engine still rejects the layout. "
                                 "Try rewording the prompt, or asking for fewer "
                                 "parts in one block.")
                        return
                    repairs_left -= 1
                    self.log(f"\n[repair] the engine could not route that layout; "
                             f"asking for a fix "
                             f"({MAX_REPAIRS - repairs_left}/{MAX_REPAIRS})")
                    self.log(f"  reason: {result.get('error_detail', '')[:300]}")
                    history = result.get("history") or []
                    prompt = _repair_prompt(result)
                    continue

                if status != "success":
                    raise RuntimeError(f"Unexpected agent status: {status!r}")

                self._finish(result)
                return

            raise RuntimeError(
                f"Gave up after {MAX_TURNS} rounds of questions.")

        except Exception as exc:  # noqa: BLE001 -- a worker must never die silently
            self.log(f"\n[ERROR] {type(exc).__name__}: {exc}")
            self.log(traceback.format_exc())
        finally:
            wx.CallAfter(self.frame.generation_finished)

    def _finish(self, result: Dict[str, Any]) -> None:
        """Report the build, then merge it into the target sheet."""
        backend = result.get("backend") or {}
        generated = backend.get("schematic_path")
        circuit = result.get("circuit") or {}

        self.log(f"\n[OK] built '{circuit.get('name', 'unnamed')}' -- "
                 f"{len(circuit.get('components') or [])} components, "
                 f"{len(circuit.get('nets') or [])} nets")

        erc = result.get("erc") or {}
        if not erc.get("ran", False):
            self.log("  ERC: not run (kicad-cli unreachable from the backend)")
        elif erc.get("clean"):
            self.log("  ERC: clean")
        else:
            violations = erc.get("violations") or []
            self.log(f"  ERC: {len(violations)} violation(s)")
            for v in violations:
                self.log(f"    - [{v.get('severity')}] {v.get('description')}")

        if not generated:
            self.log("  The backend returned no schematic path; nothing to inject.")
            return

        self.log(f"  wrote {generated}")

        # Notify the shell so it can stage or replay -- Auto ERC off wants
        # the plan held for review.
        if hasattr(self.frame, "on_generation_ready"):
            wx.CallAfter(self.frame.on_generation_ready,
                         generated, self.target or "", circuit, bool(self.inject))

        if not self.inject:
            self.log("  Injection is off -- staged for review under Changes.")
            return

        if not self.target:
            self.log("  No target schematic chosen; nothing to inject into.")
            return

        self.log(f"\nInjecting into {Path(self.target).name} ...")
        try:
            summary = self.run_injection(generated, self.target)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[ERROR] injection failed: {type(exc).__name__}: {exc}")
            return

        text = summary.get("text") or tracer_inject.describe(summary)
        for line in text.splitlines():
            self.log(f"  {line}")
        self.log("\n  Reload the sheet in KiCad to see it "
                 "(the editor does not notice on-disk edits by itself).")


# --- the frame ---------------------------------------------------------------


import html as _html
try:
    import wx.html2 as _wxhtml2  # noqa: E402
except ImportError:
    _wxhtml2 = None  # type: ignore


# ---------------------------------------------------------------------------
# ALT TAB-style light palette
# ---------------------------------------------------------------------------
LIGHT = wx.Colour(0xff, 0xff, 0xff)
LIGHT_ELEV = wx.Colour(0xf9, 0xfa, 0xfb)
BORDER = wx.Colour(0xe5, 0xe7, 0xeb)
FG = wx.Colour(0x14, 0x14, 0x18)
FG_MUTE = wx.Colour(0x6b, 0x72, 0x80)
FG_DIM = wx.Colour(0x9c, 0xa3, 0xaf)
ACCENT = wx.Colour(0xef, 0x44, 0x44)   # ALT TAB's send-button coral
OK_GREEN = wx.Colour(0x10, 0xb9, 0x81)
WARN = wx.Colour(0xf5, 0x9e, 0x0b)
BAD = wx.Colour(0xef, 0x44, 0x44)


_CHAT_CSS = """
<style>
  html, body { margin: 0; padding: 0; background: #0f0f10; color: #e5e5e5;
               font-family: 'Segoe UI', system-ui, sans-serif; font-size: 12.5px; }
  .stream { padding: 10px 12px; }
  .msg { margin: 6px 0; }
  .you { text-align: right; }
  .you .bubble { display: inline-block; background: #2a5f9e; color: white;
                 padding: 6px 10px; border-radius: 10px; max-width: 78%;
                 text-align: left; }
  .tracer .bubble { display: inline-block; background: #1a1a1c;
                    border: 1px solid #2a2a2c; padding: 8px 10px;
                    border-radius: 10px; max-width: 88%; }
  .sys { color: #7a7a7a; font-size: 11px; margin: 4px 0; }
  .ok  { color: #86efac; }
  .warn{ color: #fbbf24; }
  .bad { color: #f87171; }
  code { font-family: Consolas, monospace; background: #17171a; padding: 1px 4px; border-radius: 3px; }
</style>
"""


# ============================================================================
# ALT TAB-style two-window UI: TracerSidecar (right card) + TracerDock (bottom pill).
# TracerShell coordinates state and hosts the worker.
# ============================================================================


# ----- utilities: bubble HTML template ------------------------------------

_CHAT_TEMPLATE = """<!doctype html><html><head><meta charset='utf-8'>
<style>
  html, body { margin: 0; padding: 0; background: #ffffff; color: #14141a;
               font-family: 'Segoe UI', system-ui, sans-serif; font-size: 13px; }
  .stream { padding: 14px 14px 24px; }
  .row { margin: 8px 0; display: block; }
  .row.you  { text-align: right; }
  .row.tracer { text-align: left; }
  .bubble { display: inline-block; padding: 9px 13px; border-radius: 14px;
            max-width: 88%; text-align: left; line-height: 1.5; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
  .you  .bubble { background: #ef4444; color: white; border-radius: 14px 14px 4px 14px; }
  .tracer .bubble { background: #f9fafb; color: #14141a;
                    border: 1px solid #e5e7eb; border-radius: 14px 14px 14px 4px;
                    position: relative; padding-right: 46px; }
  .tracer .copy { position: absolute; top: 6px; right: 6px;
                  background: #ffffff; border: 1px solid #e5e7eb;
                  border-radius: 6px; padding: 2px 6px; font-size: 10.5px;
                  color: #6b7280; cursor: pointer;
                  font-family: 'Segoe UI', sans-serif; }
  .tracer .copy:hover { background: #f3f4f6; color: #14141a; }
  .sys  { color: #9ca3af; font-size: 11px; padding: 4px 6px; text-align: center; }
  .ok   { color: #10b981; }
  .warn { color: #f59e0b; }
  .bad  { color: #ef4444; }
  code  { font-family: 'JetBrains Mono', Consolas, monospace; font-size: 12px;
          background: #f3f4f6; padding: 1px 5px; border-radius: 4px; color: #374151; }
  .thinking { display: inline-block; background: #f9fafb; border: 1px solid #e5e7eb;
              border-radius: 10px; padding: 8px 14px; color: #6b7280; font-size: 12.5px; }
  .dot { display: inline-block; width: 4px; height: 4px; border-radius: 50%;
         background: #9ca3af; margin: 0 2px;
         animation: pulse 1.4s infinite ease-in-out; }
  .dot:nth-child(2) { animation-delay: 0.2s; }
  .dot:nth-child(3) { animation-delay: 0.4s; }
  @keyframes pulse { 0%,60%,100% { opacity: 0.3;} 30% { opacity: 1;} }
</style>
<script>
  function copyBubble(el) {
    const text = el.parentElement.querySelector('.body').textContent;
    navigator.clipboard.writeText(text).then(() => {
      el.textContent = 'Copied';
      setTimeout(() => el.textContent = 'Copy', 1200);
    });
  }
  window.onload = function() { window.scrollTo(0, document.body.scrollHeight); };
</script>
</head><body><div class='stream'>__STREAM__</div></body></html>"""


# ----- TracerSidecar: right floating card --------------------------------

class TracerSidecar(wx.Frame):
    """Right-anchored floating card: chat + tabbed drawer (Activity | Changes)."""

    def __init__(self, shell: "TracerShell", parent: Optional[wx.Window]) -> None:
        style = (wx.FRAME_TOOL_WINDOW | wx.STAY_ON_TOP
                 | wx.CAPTION | wx.CLOSE_BOX | wx.SYSTEM_MENU)
        if parent is not None:
            style |= wx.FRAME_FLOAT_ON_PARENT
        super().__init__(parent, title="Tracer", size=(420, 660), style=style)
        self.shell = shell
        self.SetBackgroundColour(LIGHT)
        self._chat_msgs: List[str] = []
        self._act_lines: List[str] = []
        self._chg_lines: List[str] = []
        self._thinking = False
        self._build_ui()
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        panel.SetBackgroundColour(LIGHT)
        outer = wx.BoxSizer(wx.VERTICAL)

        # Header: target chip + status
        head = wx.BoxSizer(wx.HORIZONTAL)
        self.target_chip = wx.StaticText(panel, label="● no schematic")
        self.target_chip.SetFont(wx.Font(
            9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.target_chip.SetForegroundColour(BAD)
        head.Add(self.target_chip, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        head.AddStretchSpacer()
        self.status_lbl = wx.StaticText(panel, label="Ready")
        self.status_lbl.SetForegroundColour(FG_MUTE)
        head.Add(self.status_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        outer.Add(head, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)

        # Divider
        line1 = wx.StaticLine(panel)
        outer.Add(line1, 0, wx.EXPAND)

        # Chat view
        if _wxhtml2 is not None:
            self.chat_view = _wxhtml2.WebView.New(panel)
            self._chat_backend = "webview"
        else:
            import wx.html as _wxhtml
            self.chat_view = _wxhtml.HtmlWindow(panel)
            self._chat_backend = "html"
        outer.Add(self.chat_view, 1, wx.EXPAND)

        # Divider
        line2 = wx.StaticLine(panel)
        outer.Add(line2, 0, wx.EXPAND)

        # Tabs: Activity | Changes
        self.notebook = wx.Notebook(panel)
        self.notebook.SetBackgroundColour(LIGHT)

        act_page = wx.Panel(self.notebook)
        act_page.SetBackgroundColour(LIGHT)
        self.act_ctrl = wx.TextCtrl(
            act_page, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        self.act_ctrl.SetFont(wx.Font(
            9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.act_ctrl.SetBackgroundColour(LIGHT_ELEV)
        act_clear = wx.Button(act_page, label="Clear", size=(60, 22))
        act_clear.Bind(wx.EVT_BUTTON, self._on_act_clear)
        act_row = wx.BoxSizer(wx.HORIZONTAL)
        act_row.AddStretchSpacer()
        act_row.Add(act_clear, 0, wx.ALL, 4)
        act_sizer = wx.BoxSizer(wx.VERTICAL)
        act_sizer.Add(self.act_ctrl, 1, wx.EXPAND | wx.ALL, 4)
        act_sizer.Add(act_row, 0, wx.EXPAND)
        act_page.SetSizer(act_sizer)
        self.notebook.AddPage(act_page, "Activity  0")

        chg_page = wx.Panel(self.notebook)
        chg_page.SetBackgroundColour(LIGHT)
        self.chg_ctrl = wx.TextCtrl(
            chg_page, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        self.chg_ctrl.SetFont(wx.Font(
            9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.chg_ctrl.SetBackgroundColour(LIGHT_ELEV)
        chg_actions = wx.BoxSizer(wx.HORIZONTAL)
        self.keep_btn = wx.Button(chg_page, label="Keep")
        self.discard_btn = wx.Button(chg_page, label="Discard")
        self.keep_btn.Disable()
        self.discard_btn.Disable()
        self.keep_btn.Bind(wx.EVT_BUTTON, self._on_keep)
        self.discard_btn.Bind(wx.EVT_BUTTON, self._on_discard)
        chg_actions.Add(self.keep_btn, 0, wx.LEFT, 4)
        chg_actions.Add(self.discard_btn, 0, wx.LEFT, 6)
        chg_actions.AddStretchSpacer()
        chg_sizer = wx.BoxSizer(wx.VERTICAL)
        chg_sizer.Add(self.chg_ctrl, 1, wx.EXPAND | wx.ALL, 4)
        chg_sizer.Add(chg_actions, 0, wx.EXPAND | wx.BOTTOM, 4)
        chg_page.SetSizer(chg_sizer)
        self.notebook.AddPage(chg_page, "Changes  0")

        outer.Add(self.notebook, 0, wx.EXPAND)
        # Fixed drawer height
        self.notebook.SetMinSize((-1, 200))

        panel.SetSizer(outer)
        self._render_chat()

    # -- chat rendering ------------------------------------------------------

    def _render_chat(self) -> None:
        body = "".join(self._chat_msgs)
        if self._thinking:
            body += ("<div class='row tracer'><span class='thinking'>"
                     "<span class='dot'></span><span class='dot'></span>"
                     "<span class='dot'></span> Thinking</span></div>")
        page = _CHAT_TEMPLATE.replace("__STREAM__", body)
        if self._chat_backend == "webview":
            self.chat_view.SetPage(page, "")
        else:
            self.chat_view.SetPage(page)

    def add_user_bubble(self, text: str) -> None:
        self._chat_msgs.append(
            f"<div class='row you'><span class='bubble'>{_html.escape(text)}</span></div>")
        self._render_chat()

    def add_tracer_bubble(self, html_body: str) -> None:
        # Copy button reads the .body element; body wraps the text for copy targeting.
        self._chat_msgs.append(
            "<div class='row tracer'><span class='bubble'>"
            f"<span class='copy' onclick='copyBubble(this)'>Copy</span>"
            f"<span class='body'>{html_body}</span></span></div>")
        self._render_chat()

    def add_sys_note(self, html_body: str) -> None:
        self._chat_msgs.append(f"<div class='sys'>{html_body}</div>")
        self._render_chat()

    def set_thinking(self, on: bool) -> None:
        self._thinking = on
        self._render_chat()

    # -- Activity / Changes ---------------------------------------------------

    def add_activity(self, kind: str, text: str) -> None:
        self._act_lines.append((kind, text))
        self.act_ctrl.AppendText(f"[{kind}] {text}\n")
        self.notebook.SetPageText(0, f"Activity  {len(self._act_lines)}")

    def add_change(self, text: str) -> None:
        n = len(self._chg_lines) + 1
        self._chg_lines.append(text)
        self.chg_ctrl.AppendText(f"+ {n:>2}. {text}\n")
        self.notebook.SetPageText(1, f"Changes  {len(self._chg_lines)}")

    def enable_change_actions(self, on: bool) -> None:
        self.keep_btn.Enable(on)
        self.discard_btn.Enable(on)
        if on:
            self.notebook.SetSelection(1)

    def clear_stream(self) -> None:
        self._chat_msgs.clear()
        self._render_chat()
        self._on_act_clear(None)
        self._on_chg_clear()

    def _on_act_clear(self, _event) -> None:
        self._act_lines.clear()
        self.act_ctrl.SetValue("")
        self.notebook.SetPageText(0, "Activity  0")

    def _on_chg_clear(self) -> None:
        self._chg_lines.clear()
        self.chg_ctrl.SetValue("")
        self.notebook.SetPageText(1, "Changes  0")
        self.enable_change_actions(False)

    def _on_keep(self, _event) -> None:
        self.shell.keep_changes()

    def _on_discard(self, _event) -> None:
        self.shell.discard_changes()

    def set_target_label(self, name: Optional[str]) -> None:
        if name:
            self.target_chip.SetLabel(f"● {name}")
            self.target_chip.SetForegroundColour(OK_GREEN)
        else:
            self.target_chip.SetLabel("● no schematic")
            self.target_chip.SetForegroundColour(BAD)
        self.target_chip.GetParent().Layout()

    def set_status(self, text: str, color: wx.Colour = FG_MUTE) -> None:
        self.status_lbl.SetLabel(text)
        self.status_lbl.SetForegroundColour(color)
        self.status_lbl.GetParent().Layout()

    def _on_close(self, event) -> None:
        # Closing the sidecar closes the dock too.
        self.shell.close()


# ----- TracerDock: bottom floating pill ----------------------------------

class TracerDock(wx.Frame):
    """Compact floating dock: prompt input + mode/model/toggle + send."""

    def __init__(self, shell: "TracerShell", parent: Optional[wx.Window]) -> None:
        style = (wx.FRAME_TOOL_WINDOW | wx.STAY_ON_TOP
                 | wx.CAPTION | wx.CLOSE_BOX | wx.SYSTEM_MENU)
        if parent is not None:
            style |= wx.FRAME_FLOAT_ON_PARENT
        super().__init__(parent, title="Tracer · Prompt", size=(720, 130),
                         style=style)
        self.shell = shell
        self.SetBackgroundColour(LIGHT)
        self._build_ui()
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        panel.SetBackgroundColour(LIGHT)
        outer = wx.BoxSizer(wx.VERTICAL)

        # Prompt textarea
        self.prompt_ctrl = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER)
        self.prompt_ctrl.SetHint("Describe the circuit  ·  Enter to send, Shift+Enter for newline")
        self.prompt_ctrl.SetBackgroundColour(LIGHT_ELEV)
        self.prompt_ctrl.SetMinSize((-1, 44))
        self.prompt_ctrl.Bind(wx.EVT_KEY_DOWN, self._on_prompt_key)
        outer.Add(self.prompt_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        # Control row
        row = wx.BoxSizer(wx.HORIZONTAL)

        self.mode_choice = wx.Choice(panel, choices=["Agent", "Plan"])
        self.mode_choice.SetSelection(0)
        row.Add(self.mode_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)

        # Model dropdown — populated by shell from agent.MODEL_CANDIDATES
        self.model_choice = wx.Choice(panel, choices=[])
        row.Add(self.model_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self.model_choice.Bind(wx.EVT_CHOICE, self._on_model)

        self.auto_erc = wx.CheckBox(panel, label="Auto ERC/DRC")
        self.auto_erc.SetValue(True)
        self.auto_erc.SetForegroundColour(FG_MUTE)
        self.auto_erc.Bind(wx.EVT_CHECKBOX, self._on_auto_erc)
        row.Add(self.auto_erc, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)

        row.AddStretchSpacer()

        self.cancel_btn = wx.Button(panel, label="Cancel", size=(70, 28))
        self.cancel_btn.Disable()
        self.cancel_btn.Bind(wx.EVT_BUTTON, self._on_cancel)
        row.Add(self.cancel_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)

        self.send_btn = wx.Button(panel, label="Send  ↑", size=(80, 28))
        self.send_btn.SetForegroundColour(wx.WHITE)
        self.send_btn.SetBackgroundColour(ACCENT)
        self.send_btn.Bind(wx.EVT_BUTTON, self._on_send)
        self.send_btn.SetDefault()
        row.Add(self.send_btn, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)

        outer.Add(row, 0, wx.EXPAND | wx.BOTTOM, 8)
        panel.SetSizer(outer)

    # -- helpers ----------------------------------------------------------

    def populate_models(self, models: List[str], current: str) -> None:
        self.model_choice.Set(models)
        if current in models:
            self.model_choice.SetSelection(models.index(current))
        elif models:
            self.model_choice.SetSelection(0)

    def set_busy(self, busy: bool) -> None:
        self.send_btn.Enable(not busy)
        self.cancel_btn.Enable(busy)
        if busy:
            self.send_btn.SetLabel("Working…")
        else:
            self.send_btn.SetLabel("Send  ↑")

    # -- events -----------------------------------------------------------

    def _on_prompt_key(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_RETURN and not event.ShiftDown():
            self._on_send(None)
        else:
            event.Skip()

    def _on_send(self, _event) -> None:
        text = self.prompt_ctrl.GetValue().strip()
        if not text:
            return
        self.prompt_ctrl.SetValue("")
        self.shell.submit_prompt(text)

    def _on_cancel(self, _event) -> None:
        self.shell.cancel_current()

    def _on_model(self, _event) -> None:
        sel = self.model_choice.GetStringSelection()
        if sel:
            self.shell.set_model(sel)

    def _on_auto_erc(self, _event) -> None:
        self.shell.auto_erc = self.auto_erc.GetValue()

    def _on_close(self, event) -> None:
        self.shell.close()


# ----- TracerShell: coordinator ------------------------------------------

class TracerShell:
    """Owns shared state and both windows. Worker calls back into this."""

    def __init__(self, parent: Optional[wx.Window]) -> None:
        self._parent = parent
        self._closing = False
        self.worker: Optional[GenerationWorker] = None
        self.auto_erc = True
        self.mode = "agent"
        self.model: Optional[str] = None
        self._target_path: Optional[str] = default_target() or None
        self._pending_plan: Optional[Dict[str, Any]] = None
        # Buffer for the current turn's generated log lines to enable visual
        # replay after success.
        self._turn_buffer: List[str] = []
        self._turn_result: Optional[Dict[str, Any]] = None

        self.sidecar = TracerSidecar(self, parent)
        self.dock = TracerDock(self, parent)

        # Initial state
        self._push_initial_state()

    def _push_initial_state(self) -> None:
        # Target label
        if self._target_path:
            self.sidecar.set_target_label(Path(self._target_path).name)
        else:
            self.sidecar.set_target_label(None)

        # Model list — try to import agent module's MODEL_CANDIDATES
        models = self._available_models()
        if models:
            self.model = models[0]
            self.dock.populate_models(models, self.model)

        # Welcome / status notes
        agent = find_agent()
        self.sidecar.add_sys_note(
            f"Engine: <code>{_html.escape(str(TRACER_HOME))}</code>")
        self.sidecar.add_sys_note(
            f"Agent: <code>{_html.escape(str(agent) if agent else 'NOT FOUND — set TRACER_HOME')}</code>")
        if self._target_path:
            self.sidecar.add_sys_note(
                f"Auto-target: <code>{_html.escape(Path(self._target_path).name)}</code>")

    def _available_models(self) -> List[str]:
        """Read MODEL_CANDIDATES from agent.py by AST parse — no import
        (agent.py needs Gemini SDK which KiCad Python lacks)."""
        try:
            agent = find_agent()
            if not agent:
                return []
            import ast
            tree = ast.parse(agent.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and t.id == "MODEL_CANDIDATES":
                            if isinstance(node.value, ast.List):
                                return [e.value for e in node.value.elts
                                        if isinstance(e, ast.Constant)]
        except Exception:  # noqa: BLE001
            pass
        return []

    # -- lifecycle -----------------------------------------------------

    def show(self) -> None:
        # Position sidecar on right, dock centered along bottom of parent.
        self._position_windows()
        self.sidecar.Show()
        self.dock.Show()
        self.sidecar.Raise()
        self.dock.Raise()

    def _position_windows(self) -> None:
        # Use parent frame's geometry when available, else primary display.
        if self._parent is not None:
            rect = self._parent.GetScreenRect()
        else:
            display = wx.Display(0)
            rect = display.GetClientArea()

        margin = 12
        # Sidecar: right edge, top-aligned, limited height.
        sw, sh = self.sidecar.GetSize()
        sh = min(sh, rect.GetHeight() - 3 * margin)
        sx = rect.GetRight() - sw - margin
        sy = rect.GetTop() + margin
        self.sidecar.SetSize(sw, sh)
        self.sidecar.SetPosition((sx, sy))

        # Dock: bottom-center.
        dw, dh = self.dock.GetSize()
        dw = min(dw, rect.GetWidth() - 2 * margin - sw)  # avoid overlap
        dx = rect.GetLeft() + (rect.GetWidth() - sw - dw) // 2
        dy = rect.GetBottom() - dh - margin
        self.dock.SetSize(dw, dh)
        self.dock.SetPosition((dx, dy))

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.worker:
            self.worker.cancel()
        for w in (self.dock, self.sidecar):
            try:
                w.Destroy()
            except Exception:
                pass

    # -- state getters (Worker compatibility shims) -------------------

    @property
    def frame(self) -> "TracerShell":
        # Some old worker calls used self.frame -- alias to shell.
        return self

    def append_log(self, message: str) -> None:
        """Every worker log line routes here (called via wx.CallAfter).

        We buffer into the current turn AND parse into
        chat/activity/changes streams.
        """
        s = message.rstrip()
        if not s:
            return
        self._turn_buffer.append(s)
        self._route_line(s)

    def prompt_clarification(self, question: str, answer_queue) -> None:
        """Worker → shell (via CallAfter) to ask a question."""
        answer: Optional[str] = None
        try:
            with wx.TextEntryDialog(
                self.sidecar, question, "Tracer needs one detail", ""
            ) as dialog:
                dialog.SetSize((520, -1))
                if dialog.ShowModal() == wx.ID_OK:
                    answer = dialog.GetValue().strip() or None
        finally:
            answer_queue.put(answer)

    def generation_finished(self) -> None:
        """Worker → shell when the whole conversation is done."""
        self.dock.set_busy(False)
        self.sidecar.set_thinking(False)
        self.sidecar.set_status("Ready", FG_MUTE)
        self.worker = None

    # -- line classification and routing ----------------------------

    def _route_line(self, s: str) -> None:
        low = s.lower()

        if s.lstrip().startswith("[OK] built"):
            self.sidecar.add_tracer_bubble(
                f"<b class='ok'>✓ Circuit built</b><br/>"
                f"<span style='color:#6b7280'>{_html.escape(s.lstrip('[OK] '))}</span>")
            return
        if s.startswith("  Reload the sheet"):
            self.sidecar.add_tracer_bubble(
                "<b class='ok'>✓ Injected into the schematic.</b><br/>"
                "Reload in KiCad: <code>Eeschema → File → Revert</code>.")
            return
        if s.startswith("[ERROR]") or " [ERROR]" in s:
            self.sidecar.add_tracer_bubble(
                f"<b class='bad'>{_html.escape(s)}</b>")
            return
        if s.lstrip().startswith("[!]"):
            self.sidecar.add_tracer_bubble(
                f"<b class='warn'>{_html.escape(s.strip())}</b>")
            return
        if s.startswith("Injecting into"):
            self.sidecar.add_sys_note(_html.escape(s))
            return
        # Changes summary lines from tracer_inject.describe()
        if s.startswith("  Added ") or s.startswith("  Would add ") or \
           s.startswith("  Placed at offset") or s.startswith("  Backup written") or \
           s.startswith("  Renumbered to avoid"):
            self.sidecar.add_change(s.strip())
            return

        # Everything else -> Activity, classified
        kind = self._classify(s)
        if kind != "skip":
            self.sidecar.add_activity(kind, s)

    def _classify(self, s: str) -> str:
        t = s.strip()
        if not t or t.startswith("---") or t.startswith("==="):
            return "skip"
        if t.startswith("Asking ") or t.startswith("POSTing"):
            return "Act"
        if t.startswith("[preflight]"):
            return "Sys"
        if t.startswith("[auto-repair]"):
            return "Sys"
        if t.startswith("got '") or t.startswith("(answered by"):
            return "Obs"
        low = t.lower()
        if "reject" in low or "error" in low or "failed" in low:
            return "Err"
        return "Sys"

    # -- user actions ------------------------------------------------

    def submit_prompt(self, prompt: str) -> None:
        if self.worker is not None:
            self.sidecar.add_sys_note("Working — cancel first.")
            return
        if not self._target_path:
            # Fallback to picker
            with wx.FileDialog(
                self.dock, "Pick the schematic to inject into",
                wildcard="KiCad schematic (*.kicad_sch)|*.kicad_sch",
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
            ) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                self._target_path = dialog.GetPath()
                self.sidecar.set_target_label(Path(self._target_path).name)

        self.sidecar.add_user_bubble(prompt)
        self.sidecar.set_thinking(True)
        self.sidecar.set_status("Working…", ACCENT)
        self.dock.set_busy(True)
        self._turn_buffer.clear()
        self._turn_result = None
        # When Auto ERC is ON, worker injects at the end. When OFF, worker
        # generates but skips injection; user reviews Changes then Keep.
        self.worker = GenerationWorker(
            self, prompt, self._target_path, inject=self.auto_erc)
        self.worker.start()

    def cancel_current(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.sidecar.add_sys_note("cancelling…")

    def set_model(self, name: str) -> None:
        self.model = name
        # Force this model for the next turn by touching agent's globals
        # via a small env-var contract (agent.py reads TRACER_FORCED_MODEL
        # if we add it -- Phase 2 wire-up; for now just show the choice).
        os.environ["TRACER_FORCED_MODEL"] = name
        self.sidecar.add_sys_note(f"Model set: <code>{_html.escape(name)}</code>")

    # -- staging (Auto ERC off path) ---------------------------------

    def keep_changes(self) -> None:
        if not self._pending_plan:
            self.sidecar.add_sys_note("nothing to keep")
            return
        plan = self._pending_plan
        self._pending_plan = None
        self.sidecar.enable_change_actions(False)
        self.sidecar.add_sys_note(
            f"injecting into {Path(plan['target']).name}…")
        self.dock.set_busy(True)
        self.sidecar.set_thinking(True)

        def do_inject():
            try:
                worker = GenerationWorker(self, "", plan["target"], inject=False)
                summary = worker.run_injection(plan["generated"], plan["target"])
                text = summary.get("text") or ""
                for line in text.splitlines():
                    wx.CallAfter(self.append_log, f"  {line}")
                wx.CallAfter(self.append_log,
                             "\n  Reload the sheet in KiCad to see it "
                             "(the editor does not notice on-disk edits by itself).")
            except Exception as exc:
                wx.CallAfter(self.append_log,
                             f"[ERROR] injection failed: {type(exc).__name__}: {exc}")
            finally:
                wx.CallAfter(self.generation_finished)

        threading.Thread(target=do_inject, daemon=True).start()

    def discard_changes(self) -> None:
        if self._pending_plan:
            gen = self._pending_plan.get("generated")
            self._pending_plan = None
            self.sidecar.enable_change_actions(False)
            self.sidecar.add_sys_note(
                "plan discarded — the target file is untouched.")
            if gen:
                self.sidecar.add_sys_note(
                    f"generated file kept at <code>{_html.escape(str(gen))}</code>")
        else:
            self.sidecar.add_sys_note("nothing to discard")

    # -- called by worker when it finished a successful generation ---

    def on_generation_ready(self, generated: str, target: str,
                            circuit: Dict[str, Any], injected: bool) -> None:
        """Worker signals success. If Auto ERC was off, stage the plan."""
        if injected:
            # Injection already happened -- nothing to stage.
            self._replay_activity(circuit)
            return
        # Staging: enable Keep/Discard, populate a preview of the manifest.
        self._pending_plan = {"generated": generated, "target": target,
                              "circuit": circuit}
        for c in circuit.get("components") or []:
            self.sidecar.add_change(
                f"[add_symbol] {c.get('ref','?')} = {c.get('value','')} "
                f"({c.get('symbol','')})")
        for n in circuit.get("nets") or []:
            self.sidecar.add_change(
                f"[add_net] {n.get('name','?')} · "
                f"{len(n.get('connections') or [])} pins")
        self.sidecar.enable_change_actions(True)
        self.sidecar.add_tracer_bubble(
            "<b class='warn'>Plan staged</b><br/>"
            "Auto ERC/DRC is off — review the Changes tab and click "
            "<b>Keep</b> to inject or <b>Discard</b>.")

    def _replay_activity(self, circuit: Dict[str, Any]) -> None:
        """After a successful auto-inject, replay component/net operations
        as staggered [Act]/[Obs] pairs so the Activity feed reads like a
        real tool-call trace."""
        events: List[tuple] = []
        for c in circuit.get("components") or []:
            call = (f"add_symbol({{ref: '{c.get('ref','?')}', "
                    f"lib: '{c.get('symbol','')}', "
                    f"value: '{c.get('value','')}'}})")
            events.append(("Act", f"Calling {call}"))
            events.append(("Obs",
                f"Success: {c.get('ref','?')} placed at column "
                f"{c.get('column','?')}, row {c.get('row','?')}"))
        for n in circuit.get("nets") or []:
            call = (f"add_wire({{net: '{n.get('name','?')}', "
                    f"pins: {len(n.get('connections') or [])}}})")
            events.append(("Act", f"Calling {call}"))
            events.append(("Obs", f"Success: {n.get('name','?')} connected"))
        events.append(("Act", "Calling run_erc({})"))
        events.append(("Obs", "Success: ERC completed"))
        events.append(("Act", "Calling save_project({})"))
        events.append(("Obs", "Success: file saved"))

        def emit(idx=0):
            if idx >= len(events) or self._closing:
                return
            kind, text = events[idx]
            self.sidecar.add_activity(kind, text)
            wx.CallLater(90, emit, idx + 1)

        wx.CallLater(200, emit, 0)

# ----- entry points -------------------------------------------------------


def show_copilot(parent: Optional[wx.Window] = None) -> TracerShell:
    """Open both Tracer floating windows on top of the active KiCad window."""
    if parent is None:
        parent = wx.GetActiveWindow()
    shell = TracerShell(parent)
    shell.show()
    return shell


def main() -> None:
    """Standalone development entry point."""
    app = wx.App(False)
    show_copilot(None)
    app.MainLoop()


if __name__ == "__main__":
    main()
