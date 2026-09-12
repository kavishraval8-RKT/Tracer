# Tracer Copilot — the KiCad plugin

A floating wxPython panel that turns a plain-English circuit description into a
KiCad schematic and merges it into a sheet.

Setup, install and troubleshooting all live in the repository root
[`README.md`](../README.md). This file covers only how the plugin is put
together.

## Two front doors, one UI

KiCad has no schematic equivalent of `pcbnew.ActionPlugin`, so the plugin
registers twice and shares everything behind the window:

| | Legacy ActionPlugin | KiCad 10 IPC API |
|---|---|---|
| Registered by | `__init__.py` at import | `plugin.json` |
| Appears in | PCB Editor ▸ Tools ▸ External Plugins | Schematic + PCB toolbars |
| Runs | inside `kicad.exe`, in KiCad's Python | as its own process, in a venv KiCad builds |
| Entry point | `TracerCopilotPlugin.Run` | `tracer_ipc.py` |
| Installed to | `<documents>/scripting/plugins/` | `<documents>/plugins/` |

`<documents>` is `$KICAD_DOCUMENTS_HOME/KiCad/10.0` when that variable is set,
and `~/Documents/KiCad/10.0` otherwise. `deploy_plugin.py` in the repository
root resolves both and installs to each.

## Files

| File | Role |
|---|---|
| `__init__.py` | Registers `pcbnew.ActionPlugin`; a no-op when `pcbnew` is absent |
| `plugin.json` | IPC manifest — validated against KiCad's `api.v1.schema.json` |
| `requirements.txt` | IPC-only deps. **Required**: KiCad will not mark a Python plugin ready without this file |
| `tracer_action.py` | The wx panel, the worker thread, the clarification modal, interpreter discovery |
| `tracer_inject.py` | Merges a generated sheet into an existing one |
| `tracer_ipc.py` | IPC entry point: builds its own `wx.App` and owns the main loop |

## How it reaches the engine

The agent needs the Gemini SDK, which KiCad's bundled Python does not have —
so `tracer_action.find_python()` probes for an interpreter that can import it
and runs `agent.py` there as a subprocess, one JSON object each way over
stdin/stdout. `TRACER_PYTHON` overrides the probe; `TRACER_HOME` says where the
engine lives once the plugin has been copied away from it.

Two details that are easy to break:

- `find_python()` must never return a KiCad binary. `sys.executable` inside an
  ActionPlugin is `kicad.exe` or `pcbnew.exe`, and handing one of those a `.py`
  path makes KiCad try to open the script as a project file. `_is_python_exe()`
  guards every call site.
- Subprocesses get `PYTHONHOME` / `PYTHONPATH` stripped (`child_env()`). KiCad
  points them at its own interpreter, and a child would inherit them and lose
  its own site-packages.

## Checking it without KiCad

```bash
# Imports, API connection and target detection; no GUI.
python tracer_ipc.py --selftest

# The panel standalone, outside KiCad.
python tracer_action.py
```
