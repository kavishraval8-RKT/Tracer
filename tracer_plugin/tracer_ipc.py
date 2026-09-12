"""Tracer Copilot -- KiCad 10 IPC API entry point.

This is the `entrypoint` named by plugin.json, and it is nothing like the
legacy ActionPlugin. KiCad's plugin manager runs it as a **separate process**,
using the interpreter in the per-plugin virtualenv it builds under

    %LOCALAPPDATA%\\kicad\\10.0\\python-environments\\<plugin identifier>\\

so there is no `pcbnew` module state to inherit, no KiCad main loop to hook
into, and no wx app already running. We own the process: build the wx.App
ourselves and block in MainLoop until the user closes the panel.

`wx` still imports, because that venv is created with --system-site-packages
against KiCad's own Python -- which is also why wxPython must never appear in
requirements.txt.

Everything after the window opens is the code the PCB Editor already uses:
`show_copilot` builds the panel, `default_target` works out which schematic to
inject into (over the API here, from `pcbnew.GetBoard()` there), and the
generation worker shells out to agent.py in the system Python that carries the
Gemini SDK. One UI, two front doors.

Run it by hand to check the plumbing without KiCad:

    <venv>\\Scripts\\python.exe tracer_ipc.py --selftest
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent

# KiCad launches this file by path, so the plugin directory is not necessarily
# importable. Put it first so `import tracer_action` finds our copy.
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def _log(message: str) -> None:
    """Write to stderr, which KiCad captures as 'Manager: action stderr: ...'.

    stdout is left alone: it is the one channel the plugin manager does not
    annotate, and keeping it clean makes a crash easier to read.
    """
    print(f"[tracer-ipc] {message}", file=sys.stderr, flush=True)


def _describe_environment() -> None:
    """Record what KiCad handed us. First thing to check when a launch fails."""
    _log(f"python     {sys.version.split()[0]} at {sys.executable}")
    _log(f"plugin dir {PLUGIN_DIR}")
    for var in ("KICAD_API_SOCKET", "KICAD_API_TOKEN"):
        value = os.environ.get(var)
        if var.endswith("TOKEN"):
            # Never log the token itself; only whether KiCad supplied one.
            _log(f"{var:<17} {'set' if value else 'MISSING'}")
        else:
            _log(f"{var:<17} {value or 'MISSING'}")


def _probe_api() -> None:
    """Log what the API says, without letting a failure stop the panel opening.

    Tracer edits the .kicad_sch on disk rather than through the API, so a
    broken or absent connection costs us the auto-detected target and nothing
    else -- the user can still pick the sheet by hand.
    """
    try:
        import kipy
    except ImportError:
        _log("kipy not installed; add kicad-python to requirements.txt")
        return

    try:
        kicad = kipy.KiCad()
        _log(f"connected to KiCad {kicad.get_version()}")
    except Exception as exc:
        _log(f"API connect failed ({type(exc).__name__}: {exc}); "
             f"the panel still works, you just have to pick the sheet")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _describe_environment()
    _probe_api()

    try:
        import wx

        from tracer_action import default_target, show_copilot
    except Exception:
        _log("could not import the UI:\n" + traceback.format_exc())
        return 1

    target = default_target()
    _log(f"target schematic: {target or 'none detected'}")

    if "--selftest" in argv:
        # Exercise every import and the API probe, then leave without taking
        # over the terminal with a GUI.
        _log("selftest OK")
        return 0

    app = wx.App(False)
    try:
        # No parent: KiCad's windows belong to another process entirely, so
        # there is nothing here to float this panel on top of.
        show_copilot(None)
    except Exception:
        _log("could not open the panel:\n" + traceback.format_exc())
        return 1

    app.MainLoop()
    _log("panel closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
