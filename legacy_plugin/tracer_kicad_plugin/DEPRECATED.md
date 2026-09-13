# ⚠️ DEPRECATED LEGACY PLUGIN - DO NOT USE ⚠️

## Why This Folder Exists

This folder contains a **LEGACY** `pcbnew.ActionPlugin` implementation that was used before KiCad 10's IPC plugin API existed.

## ❌ Problems with This Approach

1. **Freezes KiCad's UI thread** - The `Run()` method executes inside KiCad's wx event loop, blocking all UI interaction during sidecar launch
2. **No schematic support** - KiCad has no schematic equivalent of `pcbnew.ActionPlugin`
3. **Duplicate toolbar buttons** - If accidentally deployed alongside the IPC plugin, users see two buttons
4. **Console errors** - Legacy plugins throw import errors when loaded by the IPC system

## ✅ Correct Solution: IPC Plugin

The **correct** plugin is in `tracer_plugin/` and uses:

- `plugin.json` - KiCad 10 IPC manifest
- `tracer_ipc.py` - Entry point that launches sidecar via subprocess
- `sidecar_launcher.py` - Subprocess launch with `DETACHED_PROCESS` flags

### Deploy the IPC Plugin

```bash
python deploy.py
```

This script:
1. Eradicates all ghost installs
2. Deploys `tracer_plugin/` to `%APPDATA%\kicad\10.0\scripting\plugins\tracer_plugin`
3. Verifies the sidecar has GPU bypass and frameless window flags

## 🏛️ This Folder is Archaeology

Keep this folder for reference only. **DO NOT** copy it to any KiCad plugin directory.
