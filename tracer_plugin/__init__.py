"""Tracer Copilot -- KiCad plugin registration.

KiCad's in-process plugin API (`pcbnew.ActionPlugin`) only ever registers into
the PCB Editor's Tools menu; there is no schematic equivalent. So this package
offers two front doors and shares one UI between them:

  * `pcbnew.ActionPlugin`, registered below -- PCB Editor ▸ Tools ▸
    External Plugins ▸ Tracer Copilot. Works today, in KiCad's own Python.
  * `plugin.json`, alongside this file -- a KiCad 10 IPC API plugin declaring
    `"scopes": ["schematic"]`, which is what actually puts a Tracer button in
    the Schematic Editor.

Importing this module never fails outside KiCad: `pcbnew` and `wx` are both
optional here, so the package can be imported by tests and by the standalone
development entry point.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent

# KiCad imports plugins by path rather than installing them, so the package's
# own directory is not necessarily importable. Make the sibling modules work.
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

__version__ = "0.1.0"

try:
    import pcbnew
except ImportError:  # not inside KiCad -- nothing to register
    pcbnew = None


if pcbnew is not None:

    class TracerCopilotPlugin(pcbnew.ActionPlugin):
        """Opens the Tracer Copilot panel from the Tools menu."""

        def defaults(self) -> None:
            self.name = "Tracer Copilot"
            self.category = "Schematic generation"
            self.description = (
                "Describe a circuit in plain English; Tracer generates the "
                "KiCad schematic and merges it into a sheet."
            )
            self.show_toolbar_button = True
            icon = PLUGIN_DIR / "icon.png"
            self.icon_file_name = str(icon) if icon.is_file() else ""

        def Run(self) -> None:
            # Imported here, not at module scope: a syntax error or a missing
            # dependency in the UI should surface as a dialog when the user
            # clicks, not as a plugin that silently fails to register.
            try:
                import wx

                from tracer_action import show_copilot
            except Exception:
                self._report_import_failure()
                return

            try:
                show_copilot(wx.GetActiveWindow())
            except Exception:
                self._report_import_failure()

        @staticmethod
        def _report_import_failure() -> None:
            detail = traceback.format_exc()
            try:
                import wx

                wx.MessageBox(
                    "Tracer Copilot could not start.\n\n" + detail,
                    "Tracer", wx.OK | wx.ICON_ERROR)
            except Exception:
                print("Tracer Copilot failed to start:\n" + detail,
                      file=sys.stderr)

    # KiCad picks up plugins that register themselves at import time.
    if os.environ.get("TRACER_NO_REGISTER") != "1":
        TracerCopilotPlugin().register()
