"""⚠️ DEPRECATED LEGACY PLUGIN - DO NOT DEPLOY ⚠️

This folder contains a LEGACY pcbnew.ActionPlugin implementation that runs
INSIDE KiCad's wx event loop and FREEZES the UI during sidecar launch.

❌ DO NOT copy this folder to any KiCad plugin directory.

✅ USE the IPC plugin instead: tracer_plugin/ (deployed via deploy.py)

The IPC plugin uses KiCad 10's out-of-process plugin.json mechanism and
launches the sidecar via subprocess without blocking KiCad's UI thread.

This folder is kept only for reference/archaeology purposes.
"""

# Intentionally empty - this plugin must never register itself
