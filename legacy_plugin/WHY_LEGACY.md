# Legacy contents

Everything in this folder is **not loaded by the current plugin**. It's kept
for reference / historical continuity, not as a working code path.

## What's here

| File / dir | Was | Replaced by |
|---|---|---|
| `README.md` | Original repo README (26 KB) | Root `README.md` + `SETUP.md` |
| `tracer_kicad_plugin/` | KiCad 8 `pcbnew.ActionPlugin` legacy path (in-process; caused KiCad UI deadlocks) | `tracer_plugin/` IPC path + detached `sidecar_app.py` |
| `tracer_action.py` | The wxPython in-process UI | PySide6 `sidecar_app.py` + `assets/{dock,chat}.html` |
| `frontend.html` | Original monolithic HTML frontend (734 lines) | `assets/dock.html` + `assets/chat.html` (split, Tailwind CDN, QWebChannel) |
| `deploy_plugin.py` | First-generation deployer (single canonical root) | Root `deploy.py` (nuclear-clean; eradicates ghost installs across `C:\KiCad\…`) |
| `diagnose_plugin.py` | Dev-only diagnostic script from a debugging session | — |
| `DEPLOYMENT_SUMMARY.md` | Snapshot report from that session | — |
| `mock_ai_output.json` | Old LLM output fixture (offline testing) | Live Gemini calls |
| `test_board/`, `generated/` | Dev artifacts from generator runs | — |
| `tracer-sidecar-architecture.md` | Original architecture blueprint (Phase 1/2/3 roadmap) | We pivoted from that plan mid-development. Kept as historical context; the Phase 2 AST work (`tracer_ast/`) still traces to it. |

## Why keep any of it

- Blame / archaeology when a bug in the current stack turns out to be
  behavior inherited from here.
- `tracer-sidecar-architecture.md` still documents design decisions we
  didn't undo (embedded FastAPI, QWebEngineView shell, snapshot rollback).
- Nothing here should be imported. If a current file references anything
  under `legacy_plugin/`, that's a bug — please open an issue.
