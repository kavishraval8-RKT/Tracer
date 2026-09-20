# Tracer Copilot

Natural-language KiCad copilot. Describe a circuit in plain English; Tracer
generates the schematic and merges it into your open sheet — running its
LLM in the cloud so you don't ship an API key with the plugin.

```
"Generate an I2C sensor header — 4-pin connector, 4.7k pull-ups on SCL and SDA,
 100nF decoupling cap across VCC/GND."
```

→ Schematic drawn, ERC checked, DRC + schematic-parity reported, undo history
kept per prompt.

## Highlights

- **Cloud-backed.** No local API key needed. Sign in once via your browser,
  the plugin remembers you across launches.
- **Four modes** — Agent (generate + inject), Ask (Q&A), Plan (design
  outline, no build), Debug (verbose logs).
- **Clarification-aware routing** — when Agent asks a follow-up, your
  reply automatically continues the same turn even if the mode dropdown
  says otherwise.
- **Attachments** — click **+** to attach a PNG/JPG or capture the current
  KiCad window. Images ride the next prompt as multimodal Gemini input.
- **Per-prompt undo** — every inject writes a timestamped snapshot; the ↶
  button opens a modal to restore any earlier state.
- **DRC + schematic-parity** — every inject runs `kicad-cli pcb drc
  --schematic-parity` on the sibling `.kicad_pcb` and streams violations
  into the Activity log.
- **Quota-aware** — every send is metered against a monthly token budget
  by tier; usage bar visible on your `/billing` page.
- **Markdown-rendered replies** — headers, bold, code, lists.

## Architecture

Three components across two repos:

- **`tracer-engine`** (this repo) — the KiCad plugin + Python cloud
  backend. See [`backend/`](backend/) for the FastAPI service that
  proxies LLM calls with quota enforcement.
- **[`tracer-frontend`](https://github.com/kavishraval8-RKT/tracer_frontend)**
   — the Next.js web app: sign-in, `/activate` device-code page, `/billing`
   with the token usage dashboard, Stripe checkout.
- **Supabase** — auth, `profiles`, `device_codes`, `usage_ledger`.

Full data-flow reference: [`docs/SAAS_ARCHITECTURE.md`](docs/SAAS_ARCHITECTURE.md).

## Status

- **Windows 11 + KiCad 10** — supported and tested.
- **Linux / macOS** — not yet (plugin uses `pywin32` for window management).

## Setup

See [SETUP.md](SETUP.md) for the full guide: prerequisites, install, sign-in
flow, modes, troubleshooting, and where things live.

Quick version:

```bash
git clone https://github.com/kavishraval8-RKT/Tracer.git tracer-engine
cd tracer-engine
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
python deploy.py
```

Fully quit and relaunch KiCad. Coral ⚡ shows up in the toolbar. Click it,
click **Sign in** in the chat panel — a browser tab opens for you to link
your account. That's it.

## Updating

Already cloned? [SETUP.md → Updating an existing install](SETUP.md#updating-an-existing-install)
has the three-step loop: `git pull` → re-run `deploy.py` → fully quit +
relaunch KiCad. Any running Tracer sidecar needs to be killed so the new
code loads on next launch.

## Layout

- `SETUP.md` — full setup + walkthrough + troubleshooting.
- `backend/` — Python cloud service (FastAPI). Run locally during dev or
  deploy to Render.
- `docs/SAAS_ARCHITECTURE.md` — end-to-end architecture reference.
- `docs/ROADMAP.md` — phased plan and status.
- `docs/DEFERRED.md` — features scoped but deliberately not built (with
  reopen triggers).
- `legacy_plugin/` — old wxPython in-process route + the original
  sidecar architecture doc, kept for reference.

## License

MIT. See `tracer_plugin/plugin.json`.
