# Tracer Copilot — Setup

Tracer is a natural-language KiCad copilot. You describe a circuit in plain
English; Tracer generates the schematic, injects it into the file you have
open, and reports any DRC / schematic-parity issues.

**The plugin talks to a cloud service** for the LLM call. You sign in once
via your browser and the plugin remembers you — no local API key required.

Tested on **Windows 11 + KiCad 10 + Python 3.14**. Linux/macOS support is
not there yet (the plugin uses `pywin32` for window management).

---

## Prerequisites

| Thing | Version | How to check |
|---|---|---|
| KiCad | 10.x | `kicad-cli --version` |
| Python | 3.9+ | `python --version` |
| A Tracer account | free tier is fine | you'll create it during first sign-in |

**No Gemini or Anthropic API key required.** The cloud backend uses its
own key. Users get a monthly token budget by tier (Hobby 200k / Basic 1M /
Pro 10M).

---

## Install

```bash
git clone https://github.com/kavishraval8-RKT/Tracer.git tracer-engine
cd tracer-engine
python -m venv venv
venv\Scripts\activate          # PowerShell / cmd
pip install -r requirements.txt
```

**No `.env` needed at the plugin root** — cloud auth replaces the old
`GEMINI_API_KEY` requirement.

---

## Deploy the plugin

```bash
python deploy.py
```

`deploy.py` writes the plugin under
`%USERPROFILE%\Documents\KiCad\10.0\plugins\tracer_plugin\`
(KiCad 10's IPC plugin root), and **eradicates ghost copies** in the legacy
`C:\KiCad\...` scripting trees. Run it any time you pull new code.

Dry run:

```bash
python deploy.py --dry-run
```

After deploy, **fully quit KiCad** and relaunch. KiCad caches plugin
metadata at startup — a hot reload won't pick up the new plugin.

---

## First run — sign in

1. Open KiCad. Open any project (Schematic or PCB Editor).
2. In the toolbar you'll see a coral ⚡ icon labeled **Tracer Copilot**.
   Click it.
3. Two floating panels appear:
   - **Chat** — right edge of KiCad, showing a full-screen "Sign in to Tracer" overlay.
   - **Dock** — bottom (a 130 px pill with the prompt input).
4. Click **Sign in** in the chat overlay.
5. Your default browser opens `https://tracer.dev/activate?code=ABCD-1234`
   (or `http://localhost:3000/…` in dev). Sign in with email/password.
6. Confirm the 8-char code shown on `/activate`, click **Link the plugin**.
7. Wait ~5 seconds — the chat overlay disappears and the Tracer Copilot
   welcome screen shows. **You're signed in.** The plugin stored a token
   at `%APPDATA%\Tracer\token.json` and next launch skips this whole
   dance.

## Sending a prompt

Type a prompt in the bottom dock. Example:

> *Generate an I2C sensor header with a 4-pin connector, 4.7k pull-ups on SCL and SDA, and a 100nF decoupling cap.*

Hit Send. You'll see:

- The prompt in the chat.
- Streaming activity in the Activity tab (`[Act]`/`[Obs]`/`[Sys]`/`[Err]`).
- Component adds in the Changes tab.
- If **Auto ERC/DRC** is on and ERC passes, an auto-inject into the sch file.
- If not, a **Keep / Discard** pair.
- After inject, a Ctrl+F5 is sent to KiCad so the schematic view reloads.
- If a sibling `.kicad_pcb` exists, DRC runs with `--schematic-parity` and
  reports mismatches (press **F8** in the PCB Editor to sync).

## Account & usage

Click the small account icon in the **top-right of the chat panel** — your
default browser opens the `/billing` page. That page shows:

- Current plan (Hobby / Basic / Pro).
- **Token usage this month** — progress bar, formatted count, remaining.
- **By mode** breakdown — how much you've spent on Ask vs Plan vs Agent.
- Stripe checkout for upgrading (Basic / Pro).

Once you cross your monthly quota, the plugin's next Send returns a "Quota
exceeded" bubble pointing at that same page.

---

## Modes

The dropdown in the dock has four modes:

| Mode | What Send does |
|---|---|
| **Agent** | Full generate → backend build → auto-inject if ERC clean and Auto ERC/DRC is on |
| **Ask** | Plain Q&A. No circuit generation, no injection. Just a text reply. |
| **Plan** | Design conversation. LLM outputs a components/nets/notes outline as text — no backend build, no schematic gen, no inject. Switch to Agent to actually build the plan you agreed on. |
| **Debug** | Same as Agent, plus verbose Activity lines (mode/target/model dump, per-component ref/symbol/footprint) |

**Clarification-aware routing.** If Agent asks a follow-up ("what type of
LED?"), your next reply is automatically routed to the running Agent turn
regardless of what the mode dropdown shows. The mode dropdown greys out,
the textarea placeholder swaps to *"Answer the clarification…"*, and the
send button turns amber to signal the state. Cancel dismisses the pending
clarification.

## Attachments

Click the **+** button in the dock to attach reference material to your
next prompt. Two sources:

- **Attach file…** — file dialog, accepts PNG / JPG / JPEG / WEBP.
- **Screenshot KiCad** — captures the KiCad main window.

Chips show above the textarea. Max three attachments per turn. Cleared
after Send. PDF and text attachments are deliberately not supported yet.

## Undo

Every successful inject snapshots the `.kicad_sch` (and sibling
`.kicad_pcb` if it exists) to a timestamped `.tracer-bak.YYYYMMDD_HHMMSS`
file next to the original.

Click the **↶** button in the dock. A modal opens in the chat panel with
your prompt history, newest first. Click any entry to restore the sch
(and pcb if applicable), trim the chat history to that point, and send
Ctrl+F5 so KiCad reloads.

Ledger is in-memory only — a sidecar restart wipes it. Backup files
persist on disk.

---

## Updating an existing install

Three-step loop:

```bash
cd tracer-engine
git pull
venv\Scripts\activate
pip install -r requirements.txt   # in case a dep was added
python deploy.py
```

Then:

1. **Kill any running Tracer sidecar** so the new code loads next launch:

   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
     Where-Object { $_.CommandLine -like '*sidecar_app.py*' } |
     ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   ```

2. **Fully quit KiCad** (File → Quit; not just close the project).
3. Relaunch KiCad. Click the coral ⚡ toolbar button.

The sidecar spawns fresh from the newly deployed plugin dir. Your saved
login persists — you won't need to sign in again unless we've rotated the
JWT secret (rare) or you activated a different device on the same account
(the newer device kicks the older one out — see the SaaS architecture doc).

---

## Local development (running the cloud service yourself)

The plugin defaults point at the production URLs. If you're hacking on the
backend or frontend and want the plugin to hit **your** local dev servers,
create a `.tracer.env` file next to `tracer_auth.py`:

```
TRACER_WEB_URL=http://localhost:3000
TRACER_API_URL=http://127.0.0.1:8100
```

That file is gitignored. See:

- [`backend/README.md`](backend/README.md) — how to run the Python cloud
  service locally.
- [`docs/SAAS_ARCHITECTURE.md`](docs/SAAS_ARCHITECTURE.md) — full
  data-flow reference including the RFC 8628 device flow and the JWT
  freshness check.

---

## Troubleshooting

### No toolbar icon shows up

- Fully quit KiCad (not just close the project) and relaunch.
- Re-run `python deploy.py` and check the printed target path.
- Confirm the icon is 48×48 RGBA in
  `Documents\KiCad\10.0\plugins\tracer_plugin\icon.png`.

### Toolbar button exists but the chat panel doesn't appear

- Check the sidecar log at `logs\sidecar.log` in the repo root — startup
  errors go there.
- Check the sidecar isn't blocked by an antivirus. It's a detached
  `pythonw.exe` process.

### "Sign in to Tracer" overlay is stuck / never opens the browser

- Look for `[auth]` lines in the chat's Activity accordion:
  - `[auth] contacting https://tracer.dev …` = flow started, browser
    should open shortly. If it doesn't, copy the code from the overlay
    and paste it manually on the web page.
  - `[auth] could not reach …` = the Tracer web service is unreachable.
    Check your internet.
- If nothing at all, kill the sidecar (PowerShell one-liner in the
  Updating section) and relaunch from the KiCad toolbar.

### "Signed out. Please sign in again." after some time

- Someone activated the plugin on another device with the same account.
  Each account can have one active plugin at a time; the newer one wins.
- Click **Sign in** in the overlay to link this device again.

### "Quota exceeded" bubble

- Open the account icon (top-right of chat) → `/billing` page shows a
  progress bar with your current usage.
- Quota resets on the 1st of each UTC month.
- Upgrade at `/pricing` (Stripe checkout).

### "pick a target .kicad_sch first" on Send

You have no Schematic Editor or PCB Editor open. Tracer resolves the target
by scanning open KiCad windows first, then by walking KiCad's own file
history. Open the project in either editor and try again.

### "kicad-cli not found on PATH"

DRC uses `kicad-cli` from the KiCad install (`C:\Program Files\KiCad\10.0\bin\kicad-cli.exe`
by default). If yours is elsewhere, add it to PATH.

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
   ├─ tracer_auth.py — RFC 8628 device flow client
   ├─ tracer_api.py  — HTTP client for /v1/generate|ask|plan
   ├─ KiCadWatcher (500 ms poll) → snaps windows to KiCad geometry
   └─ Signals to KiCad via Windows keystroke send (Ctrl+F5 to reload sch)

Cloud
   ├─ tracer-frontend (Next.js on Vercel) — sign-in, /activate, /billing
   ├─ backend/       (FastAPI on Render) — /v1/* JWT-gated LLM proxy
   ├─ Supabase       — auth, profiles, device_codes, usage_ledger
   └─ Google Gemini  — LLM upstream (server-side key)
```

Why detached: KiCad's Python interpreter can't host PySide6's Qt event loop
without deadlocks. Launching as a separate process avoids the entire class
of "KiCad froze while the plugin dialog was open" bugs.

Full detail with data-flow steps, security posture, and env var reference:
[`docs/SAAS_ARCHITECTURE.md`](docs/SAAS_ARCHITECTURE.md).

DRC via `kicad-cli pcb drc --schematic-parity`. Sync (F8 equivalent) is
**not** automated — KiCad has no CLI for it. Press F8 in the PCB Editor
after inject.

Autoroute: **deliberately deferred**. See [`docs/DEFERRED.md`](docs/DEFERRED.md).

---

## Where things live

| Path | Role |
|---|---|
| `sidecar_app.py` | The PySide6 shell, watcher, bridge, DRC worker |
| `tracer_auth.py` | Device Code Flow client — login + JWT storage |
| `tracer_api.py` | HTTP client for the cloud LLM proxy |
| `tracer_plugin/` | What KiCad loads (plugin.json, tracer_ipc.py, sidecar_launcher.py, tracer_inject.py) |
| `agent.py` | LLM prompt shape (still used for local dev; deprecated for prod) |
| `app.py`, `generator.py` | Local schematic build backend (used by both local and cloud paths) |
| `backend/` | Python cloud service — FastAPI + JWT + quota + agent proxy |
| `assets/dock.html` | Bottom dock UI |
| `assets/chat.html` | Right chat UI |
| `tracer_ast/` | WIP: S-expression parser + mutators (Phase 2 of the architecture) |
| `deploy.py` | Nuclear-deploy plugin to KiCad + eradicate ghost installs |
| `docs/SAAS_ARCHITECTURE.md` | Full cloud architecture reference |
| `docs/ROADMAP.md` | Phased plan and status |
| `docs/DEFERRED.md` | Scoped-but-not-built with reopen triggers |
| `legacy_plugin/` | The old wxPython in-process route. Kept for reference; not loaded by anything. |
