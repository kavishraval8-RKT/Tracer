# Tracer

An AI copilot for KiCad 10. Describe a circuit in plain English; Tracer picks
the parts, lays them out on a real grid, routes orthogonal wires around the
symbol bodies, checks the result with KiCad's own ERC, and merges it into your
open schematic.

```
  "blink two LEDs from an ATtiny85 on 3V3"
            │
            ▼
      agent.py ──▶ Gemini ──▶ circuit JSON
            │                      │
            │  "which supply?" ◀───┘        (asks only when it must)
            ▼
      app.py  ──▶ generator.py ──▶ place ▸ route ▸ verify ▸ ERC
            │
            ▼
      .kicad_sch  ──▶ merged into your sheet
```

The layout engine is not a wrapper around an LLM's coordinates. Gemini only
chooses parts, nets, and a `(column, row)` signal-flow slot for each component;
every absolute coordinate, wire, junction and label comes from `generator.py`,
which rejects any layout it cannot route cleanly and hands the complaint back
for one repair round.

## Requirements

- **KiCad 10.0** (tested on 10.0.5)
- **Python 3.9+** for the backend — a normal install, *not* KiCad's bundled one
- A **Gemini API key** — free from <https://aistudio.google.com/apikey>

## Quick start

### 1. Get the code and install the backend

```bash
git clone <your-repo-url> tracer-engine
cd tracer-engine

python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

> Install into a normal virtualenv, never into KiCad's Python. `requirements.txt`
> deliberately omits `pcbnew` and `wxPython` — both ship inside KiCad, and a
> second copy of wx shadows the one KiCad is running.

### 2. Configure your Gemini API key

Tracer reads `GEMINI_API_KEY` from the environment, falling back to a `.env`
file in the repository root. Nothing is ever stored in the source.

```bash
cp .env.example .env     # then paste your key into .env
```

Or set it per shell:

```powershell
$env:GEMINI_API_KEY = "<your key>"      # PowerShell
```
```bash
export GEMINI_API_KEY=<your key>        # bash / zsh
```

If the key is missing, Tracer tells you so up front rather than failing
mid-generation.

> **Free-tier quota is small** — on the order of 20 requests per model per day.
> If generation starts failing with quota errors, change `MODEL_NAME` near the
> top of `agent.py`.

### 3. Install the plugin into KiCad

Easiest, and it handles both of KiCad's plugin directories for you:

```bash
python deploy_plugin.py --dry-run   # show where it will land
python deploy_plugin.py
```

To do it by hand, copy the whole `tracer_plugin/` folder into:

| Front door | Destination |
|---|---|
| PCB Editor (legacy ActionPlugin) | `Documents/KiCad/10.0/scripting/plugins/tracer_plugin/` |
| Schematic Editor (KiCad 10 IPC API) | `Documents/KiCad/10.0/plugins/tracer_plugin/` |

Two different directories, two unrelated scanners — `scripting/plugins` is
never scanned for IPC plugins, and vice versa.

> If you have set `KICAD_DOCUMENTS_HOME`, **replace `Documents/KiCad` with
> `$KICAD_DOCUMENTS_HOME/KiCad`**. KiCad will silently scan the redirected
> location and ignore your Documents folder. `deploy_plugin.py` reads that
> variable for you; `--dry-run` prints the resolved paths.

Since the plugin now lives away from the engine, tell it where the engine is:

```powershell
$env:TRACER_HOME = "C:\path\to\tracer-engine"
```

### 4. Start the backend

From the repository root, with your virtualenv active:

```bash
uvicorn app:app --reload --port 8000
```

Check it:

```bash
curl http://127.0.0.1:8000/health
```

Interactive API docs are at <http://127.0.0.1:8000/docs>.

### 5. Run it from KiCad

Restart KiCad so it picks up the plugin, then:

- **PCB Editor** → *Tools ▸ External Plugins ▸ Tracer Copilot*
- **Schematic Editor** → *Preferences ▸ Plugins ▸ Refresh Plugins*, then use
  the Tracer toolbar button

In the panel: pick the schematic to inject into (it pre-fills with the sheet
you have open), type a prompt, and press **Generate**. The log pane shows
placement, routing and verification as they happen. If Gemini needs one
detail it cannot reasonably default, a dialog asks and the conversation
resumes.

Try:

> Build an ATtiny85 board powered from a 2-pin 5V header through an AMS1117-3.3
> regulator. Decouple the regulator input and output, decouple the MCU supply,
> add a 10k pull-up on RESET, and drive two indicator LEDs from PB0 and PB1
> through 330 ohm series resistors.

## Repository layout

```
tracer-engine/
├── agent.py             prompt ▸ Gemini ▸ circuit JSON, with clarification
│                        rounds and one layout-repair round
├── app.py               FastAPI service wrapping the engine
├── generator.py         the layout engine: placement, routing, verification, ERC
├── deploy_plugin.py     installs tracer_plugin/ into KiCad's plugin folders
├── mock_ai_output.json  sample payload + the circuit JSON schema
├── metadata.json        KiCad PCM package manifest
├── requirements.txt     backend dependencies
├── .env.example         copy to .env and add your key
└── tracer_plugin/
    ├── __init__.py          registers the legacy pcbnew.ActionPlugin
    ├── plugin.json          KiCad 10 IPC manifest
    ├── requirements.txt     IPC-only deps, installed by KiCad itself
    ├── tracer_action.py     the wx panel, worker thread, agent plumbing
    ├── tracer_inject.py     merges a generated sheet into an existing one
    └── tracer_ipc.py        IPC entry point (runs out of process)
```

## Using the backend directly

The engine works without the plugin or an API key:

```bash
# Build the sample circuit straight from JSON
python generator.py mock_ai_output.json out.kicad_sch

# Or drive the agent from a terminal (needs GEMINI_API_KEY)
python agent.py "a 555 astable blinking an LED at 1Hz"
```

## Troubleshooting

**"No Gemini API key found"** — `GEMINI_API_KEY` is not set and there is no
`.env`. See step 2; the error message lists the exact commands.

**Plugin missing from the PCB Editor's Tools menu** — it is in the wrong
directory. Run `python deploy_plugin.py --dry-run` and compare. Remember
`KICAD_DOCUMENTS_HOME`.

**Preferences ▸ Plugins is empty, with no error row** — the IPC scanner found
nothing, or found the plugin and could not finish preparing it. `tracer_plugin/`
must be under `<documents>/plugins/`, and it must contain `requirements.txt` —
KiCad refuses to mark a Python plugin ready without one and reports nothing in
the table when it bails.

**"does not appear to be a KiCad project file"** — the plugin handed a `.py`
path to a KiCad executable instead of a Python interpreter. Set `TRACER_PYTHON`
to a real `python.exe` that has the Gemini SDK.

**"Could not find agent.py"** — set `TRACER_HOME` to the repository root.

**Backend rejects the layout at the verification stage** — the router could not
find a clean path. The repair round usually fixes it; if not, raise
`channel_mm` / `body_margin_mm` in the request's `layout` block.

## Known limitations

- `kipy.schematic` cannot be imported in `kicad-python` 0.8.0 (an upstream
  `PageSettings` bug), so Tracer edits `.kicad_sch` files on disk rather than
  through the IPC API. It only asks the API *which* file is open.
- KiCad 10.0.5's IPC API does not return the open schematic's absolute path, so
  Tracer resolves it from the Schematic Editor's recent-file list. If the
  pre-filled target is ever wrong, use **Browse**.
- `agent.py` targets Google's legacy `google-generativeai` SDK, which is
  deprecated upstream in favour of `google-genai`. Migration is not done yet.
- A block much past a dozen components runs off an A4 sheet. Split large
  designs.

## License

GPL-3.0-or-later, matching KiCad. See `metadata.json`.
