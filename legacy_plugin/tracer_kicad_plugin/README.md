# Tracer Copilot — KiCad Plugin

One-click launcher for Tracer's floating overlay from inside KiCad.

## What it does

Adds a **Tracer Copilot** button to the PCB Editor toolbar. Clicking it
spawns Tracer's sidecar overlay in a separate process — the overlay then
finds KiCad's window and snaps itself to it, auto-detects the currently
open `.kicad_sch`, and lets you prompt-generate circuits into it without
leaving KiCad's window.

Phase 1 scaffold: **launcher only**. The sidecar (`sidecar_app.py`) runs
externally in a system Python interpreter that has PySide6 + Gemini SDK
installed — KiCad's bundled Python cannot host the full app.

## Install (dev)

Symlink or copy this folder into KiCad's plugin directory:

**Windows**

```powershell
$dst = "$env:USERPROFILE\Documents\KiCad\10.0\scripting\plugins\tracer_copilot"
New-Item -ItemType SymbolicLink -Path $dst -Target "C:\tracer-engine\tracer_kicad_plugin"
```

**macOS / Linux**

```bash
ln -s "$(pwd)/tracer_kicad_plugin" ~/Documents/KiCad/10.0/scripting/plugins/tracer_copilot
```

Restart KiCad → open **PCB Editor** → the Tracer icon appears in the
toolbar (or under **Tools → External Plugins**).

## Configuration

Two environment variables let you override where the launcher looks:

- `TRACER_HOME` — path to the `tracer-engine` repo root (contains
  `sidecar_app.py`). Default: parent of this plugin folder.
- `TRACER_PYTHON` — full path to a Python interpreter that has PySide6,
  google-generativeai, uvicorn, and kicad_sch_api installed. Default:
  auto-detects `C:\PythonNNN\python.exe`, then falls back to `python`
  on PATH.

Example (Windows):

```powershell
setx TRACER_HOME "C:\tracer-engine"
setx TRACER_PYTHON "C:\Python313\python.exe"
```

## Uninstall

Remove the symlink / folder from KiCad's plugin directory and restart.

## Roadmap

- Phase 1 (this) — external overlay, auto-snap to KiCad, launcher plugin.
- Phase 2 — in-process wxPython panel with live tool-streaming (`add_wire`,
  `add_symbol`, `run_erc`, `run_drc`). Requires porting the sidecar's UI
  from Qt/HTML to wxPython so it can run inside KiCad's Python interpreter.
