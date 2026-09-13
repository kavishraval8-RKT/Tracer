"""Tracer Copilot -- KiCad 10 IPC API entry point.

KiCad launches this out-of-process for the schematic/PCB toolbar buttons
declared in plugin.json. It starts the external PySide6 sidecar overlay.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent

if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def _log(message: str) -> None:
    print(f"[tracer-ipc] {message}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _log(f"python {sys.version.split()[0]} at {sys.executable}")
    _log(f"plugin dir {PLUGIN_DIR}")

    if "--selftest" in argv:
        from sidecar_launcher import resolve_python, sidecar_path
        _log(f"sidecar {sidecar_path() or 'MISSING'}")
        _log(f"python {resolve_python() or 'MISSING'}")
        _log("selftest OK")
        return 0

    try:
        from sidecar_launcher import launch_sidecar
    except Exception:
        _log("could not import sidecar launcher:\n" + traceback.format_exc())
        return 1

    try:
        launch_sidecar()
    except Exception:
        _log("sidecar launch failed:\n" + traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
