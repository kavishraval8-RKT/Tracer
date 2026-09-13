# Tracer Copilot — Setup

Tracer is a natural-language KiCad copilot. You describe a circuit in plain
English; Tracer generates the schematic, injects it into the file you have
open, and reports any DRC / schematic-parity issues on the board.

Tested on **Windows 11 + KiCad 10 + Python 3.14**. The plugin path is
Windows-specific in a few places (window handles, `pywin32`) — Linux/macOS
support is not there yet.

---

## Prerequisites

| Thing | Version | How to check |
|---|---|---|
| KiCad | 10.x | `kicad-cli --version` |
| Python | 3.9+ | `python --version` |
| Gemini API key | any | https://aistudio.google.com/apikey |
| Java (optional) | 11+ | Only if you later enable Freerouting (deferred) |

Windows-only Python packages (`pywin32`) are required — the plugin talks to
the KiCad window handle directly.

---

## Install

```bash
git clone https://github.com/kavishraval8-RKT/tracer-engine.git
cd tracer-engine
python -m venv venv
venv\Scripts\activate     # PowerShell / cmd
pip install -r requirements.txt
```

Add your Gemini key. Copy `.env.example` → `.env` and set:

```
GEMINI_API_KEY=your_actual_key_here
```

Sanity check:

```bash
python agent.py --check-key
```

Should print a green `key OK`.

---

## Deploy the plugin

```bash
python deploy.py
```

`deploy.py` writes the plugin under `%USERPROFILE%\Documents\KiCad\10.0\plugins\tracer_plugin\`
(KiCad 10's IPC plugin root), and **eradicates ghost copies** in the legacy
`C:\KiCad\...` scripting trees. Run it any time you pull new code.

Dry run to see what it would touch without changing anything:

```bash
python deploy.py --dry-run
```

After deploy, **fully quit KiCad** (not just close the project) and relaunch.
KiCad caches plugin metadata at startup — a hot reload will not pick up the
new plugin.

---

## First run

1. Open KiCad. Open any project (Schematic Editor or PCB Editor — either
   works; Tracer auto-detects the target).
2. In the toolbar you'll see a coral ⚡ icon labeled **Tracer Copilot**.
   Click it.
3. Two floating panels appear:
   - **Chat** — right edge of the KiCad window (620 px tall).
   - **Dock** — bottom (a 130 px pill with the prompt input).
4. Type a prompt. Example:
   > *Generate an I2C sensor header with a 4-pin connector, 4.7k pull-ups on SCL and SDA, and a 100nF decoupling cap.*
5. Hit Send. You'll see:
   - The prompt in the chat.
   - Streaming activity in the Activity tab (`[Act]`/`[Obs]`/`[Sys]`/`[Err]`).
   - Component adds in the Changes tab.
   - If **Auto ERC/DRC** is on and ERC passes, an auto-inject into the sch file.
   - If not, a **Keep / Discard** pair.
   - After inject, a Ctrl+F5 is sent to KiCad so the schematic view reloads.
   - If a sibling `.kicad_pcb` exists, DRC runs with `--schematic-parity` and
     reports mismatches (press **F8** in the PCB Editor to sync).

---

## Modes

The dropdown in the dock has four modes:

| Mode | What Send does |
|---|---|
| **Agent** | Full generate → backend build → auto-inject if ERC clean and Auto ERC/DRC is on |
| **Ask** | Plain Q&A. No circuit generation, no injection. Just a text reply. |
| **Plan** | Full generate + backend build (so you see all Changes) but never auto-injects. Always shows Keep/Discard. |
| **Debug** | Same as Agent, plus verbose Activity lines (mode/target/model dump, per-component ref/symbol/footprint) |

---

## Undo

Every successful inject snapshots the `.kicad_sch` (and sibling `.kicad_pcb`
if it exists) to a timestamped `.tracer-bak.YYYYMMDD_HHMMSS` file next to
the original.

Click the **↶** button in the dock. A modal opens in the chat panel with
your prompt history, newest first: `timestamp · MODE · prompt`. Click any
entry to:

- Restore the sch (and pcb if applicable) from that snapshot
- Drop later ledger entries and trim the chat history to that point
- Send Ctrl+F5 so KiCad reloads

The ledger is in-memory only — a sidecar restart wipes it. Backup files
persist on disk; you can manually restore any of them by renaming.

---

## Forcing a model

Rotation across Gemini models is automatic when one hits a quota. To pin a
specific model:

```powershell
$env:TRACER_FORCED_MODEL = "gemini-2.5-pro"
```

The dock's model dropdown also lets you pick per-session.

---

## Troubleshooting

### No toolbar icon shows up

- Fully quit KiCad (not just close the project) and relaunch.
- Re-run `python deploy.py` and check the printed target path.
- Confirm the icon is under 64 KB, RGBA, and referenced correctly in
  `tracer_plugin/plugin.json` (`"icons-light"` and `"icons-dark"` — not
  `"icon"`; KiCad 10 silently ignores the singular form).

### Toolbar button exists but the panels don't appear

- Check `Documents/KiCad/10.0/plugins/tracer_plugin/tracer_ipc.py` runs:
  ```bash
  python <that path> --selftest
  ```
- Check the sidecar isn't blocked by an antivirus. It's a detached
  `pythonw.exe` process.

### "pick a target .kicad_sch first" on Send

You have no Schematic Editor or PCB Editor open. Tracer resolves the target
by scanning open KiCad windows first, then by walking KiCad's own file
history (`AppData\Roaming\kicad\10.0\eeschema.json` and `pcbnew.json`).
Open the project in either editor and try again.

### "kicad-cli not found on PATH"

DRC uses `kicad-cli` from the KiCad install (`C:\Program Files\KiCad\10.0\bin\kicad-cli.exe`
by default). If yours is elsewhere, add it to PATH.

### Gemini API errors

- `key OK` from `python agent.py --check-key` proves the key works.
- Free-tier daily limits are per-model; if one hits quota the shell
  rotates to the next in `MODEL_CANDIDATES`.

---

## Architecture (one-page tour)

```
KiCad (running)
   │
   │ toolbar click → runs tracer_plugin/tracer_ipc.py
   ▼
tracer_ipc.py (a shim; imports sidecar_launcher.py)
   │
   │ subprocess.Popen(sidecar_app.py) DETACHED
   ▼
sidecar_app.py (PySide6 process, separate from KiCad)
   ├─ QWebEngineView × 2 (dock, chat) — HTML in assets/{dock,chat}.html
   ├─ QWebChannel bridge → JS <-> Python
   ├─ FastAPI/Uvicorn on 127.0.0.1:8000 (agent + generator + inject backend)
   ├─ KiCadWatcher (500 ms poll) → snaps windows to KiCad geometry
   └─ Signals to KiCad via Windows keystroke send (Ctrl+F5 to reload sch)
```

Why detached: KiCad's Python interpreter can't host PySide6's Qt event loop
without deadlocks. Launching as a separate process avoids the entire class
of "KiCad froze while the plugin dialog was open" bugs.

DRC via `kicad-cli pcb drc --schematic-parity`. Sync (F8 equivalent) is
**not** automated — KiCad has no CLI for it. Press F8 in the PCB Editor
after inject.

Autoroute: **deliberately deferred**. See [`docs/DEFERRED.md`](docs/DEFERRED.md)
for the scope and reopen triggers.

---

## Where things live

| Path | Role |
|---|---|
| `agent.py` | LLM prompt shape, model rotation, `_choose_model`, `ask_gemini` |
| `app.py` | FastAPI backend endpoints |
| `generator.py` | Circuit spec → schematic AST |
| `sidecar_app.py` | The PySide6 shell, watcher, bridge, DRC worker |
| `tracer_plugin/` | What KiCad loads (plugin.json, tracer_ipc.py, sidecar_launcher.py, tracer_inject.py) |
| `assets/dock.html` | Bottom dock UI |
| `assets/chat.html` | Right chat UI |
| `tracer_ast/` | WIP: S-expression parser + mutators (Phase 2 of the architecture — see legacy_plugin/tracer-sidecar-architecture.md) |
| `deploy.py` | Nuclear-deploy plugin to KiCad + eradicate ghost installs |
| `docs/DEFERRED.md` | Scoped-but-not-built work with reopen triggers |
| `legacy_plugin/` | The old wxPython in-process route + historical docs. Kept for reference; not loaded by anything. |
