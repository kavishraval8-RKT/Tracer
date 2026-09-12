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

        if not self.inject:
            self.log("  Injection is off -- the block was left as its own file.")
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


class TracerFrame(wx.Frame):
    """The floating Copilot panel."""

    def __init__(self, parent: Optional[wx.Window] = None) -> None:
        # FRAME_FLOAT_ON_PARENT asserts outright when there is no parent, so it
        # only goes on when the panel really is floating over a KiCad window.
        style = wx.DEFAULT_FRAME_STYLE
        if parent is not None:
            style |= wx.FRAME_FLOAT_ON_PARENT
        super().__init__(parent, title="Tracer Copilot", size=(760, 660),
                         style=style)
        self.worker: Optional[GenerationWorker] = None
        self._build_ui()
        self.Centre()

    # -- construction ---------------------------------------------------------

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        heading = wx.StaticText(panel, label="Describe the circuit you want")
        font = heading.GetFont()
        font.SetPointSize(font.GetPointSize() + 1)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        heading.SetFont(font)
        outer.Add(heading, 0, wx.ALL, 10)

        self.prompt_ctrl = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER, size=(-1, 110),
            value=("An ATtiny85 blinker running from a 2-pin 5V header through "
                   "an AMS1117-3.3, with two indicator LEDs."),
        )
        self.prompt_ctrl.SetHint("e.g. a 555 astable running at 1 Hz")
        outer.Add(self.prompt_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # -- target sheet
        target_box = wx.BoxSizer(wx.HORIZONTAL)
        target_box.Add(wx.StaticText(panel, label="Inject into:"),
                       0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.target_ctrl = wx.TextCtrl(panel, value=default_target() or "")
        target_box.Add(self.target_ctrl, 1, wx.ALIGN_CENTER_VERTICAL)
        browse = wx.Button(panel, label="Browse...", size=(90, -1))
        browse.Bind(wx.EVT_BUTTON, self.on_browse)
        target_box.Add(browse, 0, wx.LEFT, 6)
        outer.Add(target_box, 0, wx.EXPAND | wx.ALL, 10)

        # -- actions
        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.inject_ctrl = wx.CheckBox(
            panel, label="Inject into the sheet above (otherwise leave a standalone file)")
        self.inject_ctrl.SetValue(True)
        actions.Add(self.inject_ctrl, 0, wx.ALIGN_CENTER_VERTICAL)
        actions.AddStretchSpacer()

        self.generate_btn = wx.Button(panel, label="Generate")
        self.generate_btn.Bind(wx.EVT_BUTTON, self.on_generate)
        self.generate_btn.SetDefault()
        actions.Add(self.generate_btn, 0, wx.RIGHT, 6)

        self.cancel_btn = wx.Button(panel, label="Cancel")
        self.cancel_btn.Bind(wx.EVT_BUTTON, self.on_cancel)
        self.cancel_btn.Disable()
        actions.Add(self.cancel_btn)
        outer.Add(actions, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # -- console
        outer.Add(wx.StaticText(panel, label="Log"), 0, wx.LEFT | wx.BOTTOM, 10)
        self.console = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.HSCROLL)
        self.console.SetFont(wx.Font(
            9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        outer.Add(self.console, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        self.status = wx.StaticText(panel, label="Ready.")
        outer.Add(self.status, 0, wx.ALL, 10)

        panel.SetSizer(outer)

        self.append_log("Tracer Copilot ready.")
        self.append_log(f"  engine home : {TRACER_HOME}")
        agent = find_agent()
        self.append_log(f"  agent       : {agent or 'NOT FOUND -- set TRACER_HOME'}")

    # -- logging --------------------------------------------------------------

    def append_log(self, message: str) -> None:
        """Append to the console. GUI thread only -- workers use wx.CallAfter."""
        self.console.AppendText(message.rstrip("\n") + "\n")
        self.console.ShowPosition(self.console.GetLastPosition())

    # -- events ---------------------------------------------------------------

    def on_browse(self, _event: wx.CommandEvent) -> None:
        current = self.target_ctrl.GetValue()
        with wx.FileDialog(
            self, "Choose the schematic to inject into",
            defaultDir=str(Path(current).parent if current else TRACER_HOME),
            wildcard="KiCad schematic (*.kicad_sch)|*.kicad_sch",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.target_ctrl.SetValue(dialog.GetPath())

    def on_generate(self, _event: wx.CommandEvent) -> None:
        prompt = self.prompt_ctrl.GetValue().strip()
        if not prompt:
            wx.MessageBox("Describe the circuit first.", "Tracer",
                          wx.OK | wx.ICON_INFORMATION, self)
            return

        inject = self.inject_ctrl.GetValue()
        target = self.target_ctrl.GetValue().strip() or None
        if inject and not target:
            wx.MessageBox(
                "Choose a schematic to inject into, or untick injection.",
                "Tracer", wx.OK | wx.ICON_INFORMATION, self)
            return

        self.console.SetValue("")
        self.generate_btn.Disable()
        self.cancel_btn.Enable()
        self.status.SetLabel("Working...")

        self.worker = GenerationWorker(self, prompt, target, inject)
        self.worker.start()

    def on_cancel(self, _event: wx.CommandEvent) -> None:
        if self.worker:
            self.worker.cancel()
            self.append_log("\n[cancelling after this step...]")

    def prompt_clarification(
        self, question: str, answer_queue: "queue.Queue[Optional[str]]"
    ) -> None:
        """Ask the user the agent's question. GUI thread, via wx.CallAfter.

        The worker is parked on `answer_queue`, so every path out of here must
        put something on it -- including the cancel path.
        """
        answer: Optional[str] = None
        try:
            with wx.TextEntryDialog(
                self, question, "Tracer needs one detail", ""
            ) as dialog:
                dialog.SetSize((520, -1))
                if dialog.ShowModal() == wx.ID_OK:
                    answer = dialog.GetValue().strip() or None
        finally:
            answer_queue.put(answer)

    def generation_finished(self) -> None:
        self.generate_btn.Enable()
        self.cancel_btn.Disable()
        self.status.SetLabel("Ready.")
        self.worker = None


# --- entry points ------------------------------------------------------------


def show_copilot(parent: Optional[wx.Window] = None) -> TracerFrame:
    """Open the panel on top of whatever KiCad window is in front."""
    if parent is None:
        parent = wx.GetActiveWindow()
    frame = TracerFrame(parent)
    frame.Show()
    frame.Raise()
    return frame


def main() -> None:
    """Standalone development entry point."""
    app = wx.App(False)
    show_copilot(None)
    app.MainLoop()


if __name__ == "__main__":
    main()
