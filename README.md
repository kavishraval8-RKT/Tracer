# Tracer Copilot

Natural-language KiCad copilot. Describe a circuit in plain English; Tracer
generates the schematic and merges it into your open sheet.

```
"Generate an I2C sensor header — 4-pin connector, 4.7k pull-ups on SCL and SDA,
 100nF decoupling cap across VCC/GND."
```

→ Schematic drawn, ERC checked, DRC + schematic-parity reported, undo history
kept per prompt.

## Highlights

- **Four modes** — Agent (generate + inject), Ask (Q&A), Plan (design
  outline, no build), Debug (verbose logs).
- **Clarification-aware routing** — when Agent asks a follow-up, your reply
  automatically continues the same turn even if the mode dropdown says
  otherwise. Dropdown greys out; send button turns amber.
- **Attachments** — click **+** to attach a PNG/JPG or capture the current
  KiCad window. Images ride the next prompt as multimodal Gemini input.
- **Per-prompt undo** — every inject writes a timestamped snapshot; the
  ↶ button opens a modal to restore any earlier state.
- **DRC + schematic-parity** — every inject runs `kicad-cli pcb drc
  --schematic-parity` on the sibling `.kicad_pcb` and streams violations
  into the Activity log.
- **Markdown-rendered replies** — headers, bold, code, lists.

## Status

- **Windows 11 + KiCad 10** — supported and tested
- **Linux / macOS** — not yet (plugin uses `pywin32` for window management)

## Setup

See [SETUP.md](SETUP.md) for the full guide: prerequisites, install, deploy,
first-run walkthrough, modes, troubleshooting, and architecture.

Quick version:

```bash
git clone https://github.com/kavishraval8-RKT/tracer-engine.git
cd tracer-engine
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # then edit .env and paste your GEMINI_API_KEY
python deploy.py
```

Fully quit and relaunch KiCad. Coral ⚡ shows up in the toolbar.

## Updating

Already cloned? [SETUP.md → Updating an existing install](SETUP.md#updating-an-existing-install)
has the three-step loop: `git pull` → re-run `deploy.py` → fully quit +
relaunch KiCad. Any running Tracer sidecar needs to be killed so the new
code loads on next launch.

## Layout

- `SETUP.md` — full setup + walkthrough + troubleshooting
- `docs/DEFERRED.md` — features scoped but deliberately not built (with
  reopen triggers)
- `legacy_plugin/` — old wxPython in-process route + the original
  sidecar architecture doc, kept for reference

## License

MIT. See `tracer_plugin/plugin.json`.
