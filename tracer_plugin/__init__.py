"""Tracer Copilot -- package marker for tests and tooling.

The PCB/schematic toolbar button is registered by the KiCad 10 IPC plugin
(``plugin.json`` + ``tracer_ipc.py``) under ``plugins/tracer_plugin/``.
Do NOT register a legacy ``pcbnew.ActionPlugin`` here — it creates a
duplicate toolbar button that runs inside KiCad's wx thread and freezes the
UI while launching the sidecar.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent

if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

__version__ = "0.1.0"
