# Tracer

An AI copilot for KiCad 10. Describe a circuit in plain English; Tracer picks
the parts, places them on a real grid, routes orthogonal wires around the
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

**Tracer is not a wrapper that asks an LLM for coordinates.** Gemini chooses
only the parts, the nets, and a `(column, row)` signal-flow slot for each
component. Every absolute coordinate, wire, junction, label and power symbol
comes from `generator.py`, which refuses any layout it cannot route cleanly and
hands the complaint back to Gemini for one repair round. The result is checked
by KiCad's own ERC before you ever see it.

---

## Table of contents

- [Before you start](#before-you-start)
- [Setup](#setup)
  - [Step 1 — Get the code](#step-1--get-the-code)
  - [Step 2 — Install the backend](#step-2--install-the-backend)
  - [Step 3 — Configure your Gemini API key](#step-3--configure-your-gemini-api-key)
  - [Step 4 — Install the plugin into KiCad](#step-4--install-the-plugin-into-kicad)
  - [Step 5 — Point the plugin at the engine](#step-5--point-the-plugin-at-the-engine)
  - [Step 6 — Start the backend](#step-6--start-the-backend)
  - [Step 7 — Pre-flight check](#step-7--pre-flight-check)
- [Using it in KiCad](#using-it-in-kicad)
- [Your first circuit](#your-first-circuit)
- [Writing prompts that work](#writing-prompts-that-work)
- [Troubleshooting](#troubleshooting)
- [Using the engine without KiCad](#using-the-engine-without-kicad)
- [How it works inside](#how-it-works-inside)
- [Repository layout](#repository-layout)
- [Known limitations](#known-limitations)

---

## Before you start

You need four things. Check each one now — it saves debugging later.

| Requirement | Check it | Expected |
|---|---|---|
| **KiCad 10.0** | `kicad-cli version` | `10.0.x` |
| **Python 3.9+**, a normal install — *not* KiCad's | `python --version` | `3.9` or newer |
| **pip** | `python -m pip --version` | any version |
| **A Gemini API key** | free at <https://aistudio.google.com/apikey> | — |

> **Windows: which Python?**
> If `python --version` opens the Microsoft Store, you don't have a usable
> Python. Install from <https://python.org> and tick **"Add python.exe to
> PATH"** during setup.

> **Do not install anything into KiCad's Python.** KiCad ships its own
> interpreter with `pcbnew` and `wxPython` built in. Tracer's backend runs in a
> separate, normal Python, and the plugin bridges the two. Installing Tracer's
> dependencies into KiCad's Python can break KiCad itself.

Roughly 10 minutes for the whole setup.

---

## Setup

### Step 1 — Get the code

```bash
git clone https://github.com/kavishraval8-RKT/Tracer.git
cd Tracer
```

Everything below runs from this folder, referred to as the **repository root**.

### Step 2 — Install the backend

Create a virtual environment so Tracer's packages stay out of your system
Python:

```powershell
# Windows PowerShell
python -m venv venv
.\venv\Scripts\Activate.ps1
```
```bash
# macOS / Linux
python3 -m venv venv
source venv/bin/activate
```

Your prompt should now start with `(venv)`. Then:

```bash
pip install -r requirements.txt
```

<details>
<summary>PowerShell refuses to activate: "running scripts is disabled"</summary>

Windows blocks script execution by default. Allow it for your user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Then run the activate line again. Or use `.\venv\Scripts\activate.bat` from
`cmd.exe` instead.

</details>

**Checkpoint** — this should print `deps ok`, not a traceback:

```bash
python -c "import fastapi, kicad_sch_api, google.generativeai; print('deps ok')"
```

### Step 3 — Configure your Gemini API key

You need **your own** key. It is free and takes a minute. It is never
committed — `.env` is gitignored and no key is ever stored in the source.

#### 3a. Get a key

1. Go to <https://aistudio.google.com/apikey> and sign in with a Google account.
2. Click **Create API key**.
3. Copy it. It is a long string starting with `AI` or `AQ`.

Treat it like a password. Don't paste it into chats, screenshots or issues.

#### 3b. Put it in a `.env` file

From the repository root:

```powershell
Copy-Item .env.example .env    # Windows PowerShell
```
```bash
cp .env.example .env           # macOS / Linux
```

Open `.env` in any text editor and replace the placeholder so the line reads:

```
GEMINI_API_KEY=AIza...your-actual-key...
```

No quotes, no spaces around the `=`, nothing else to change. Both the backend
and the KiCad plugin read this file, so you don't need to set anything
per-terminal.

<details>
<summary>Prefer an environment variable instead of a file?</summary>

That works too, and it takes precedence over `.env`:

```powershell
# Windows, this terminal only
$env:GEMINI_API_KEY = "<your key>"

# Windows, permanently (reopen your terminal afterwards)
[Environment]::SetEnvironmentVariable('GEMINI_API_KEY','<your key>','User')
```
```bash
# macOS / Linux, this shell only
export GEMINI_API_KEY=<your key>

# macOS / Linux, permanently
echo 'export GEMINI_API_KEY=<your key>' >> ~/.bashrc   # or ~/.zshrc
```

`.env` is usually easier, because the KiCad plugin launches Python as a
subprocess and will not always inherit a variable you set in one terminal.

</details>

#### 3c. Check the key works

```bash
python agent.py --check-key
```

```
[ok]   key found in /path/to/Tracer/.env
       AIzaSy...Ab12  (39 characters)
[..]   asking Gemini to confirm it (gemini-3.5-flash)...
[ok]   Gemini accepted the key. You are ready to go.
```

It exits `0` on success and `1` on failure, and it tells you **which** file or
variable the key came from — the thing you actually need to know when it isn't
working.

| What it prints | What to do |
|---|---|
| `No Gemini API key found` + `There is no ...\.env yet` | You skipped 3b. Copy `.env.example` to `.env`. |
| `...\.env exists but carries no usable key` | The file still holds `your_gemini_api_key_here`. Paste your real key over it. |
| `Gemini rejected the API key` | Key is wrong or revoked. Re-copy from AI Studio; check for a trailing space or stray quotes. |
| `[note] .env also sets GEMINI_API_KEY; the environment variable wins` | A stale shell variable is shadowing your file. Fix it, or clear it: `Remove-Item Env:GEMINI_API_KEY` / `unset GEMINI_API_KEY`. |
| `Gemini has no model called ...` | Your key's project doesn't offer that model. Change `MODEL_NAME` near the top of `agent.py`. |
| `Gemini rate limit reached` | See the quota note below. |

> **Free-tier quota is small** — roughly **20 requests per model per day**. Each
> generation is one request; a clarification round or a layout repair is
> another. When you hit the cap it resets tomorrow. To keep working now, change
> `MODEL_NAME` near the top of `agent.py` to a different Gemini model, which
> gets its own quota.

### Step 4 — Install the plugin into KiCad

KiCad has **two** plugin systems and Tracer registers with both. They live in
two unrelated directories, and neither scanner looks at the other's:

| Front door | Goes in | Gives you |
|---|---|---|
| Legacy ActionPlugin | `<documents>/scripting/plugins/tracer_plugin/` | PCB Editor ▸ Tools ▸ External Plugins |
| KiCad 10 IPC API | `<documents>/plugins/tracer_plugin/` | Schematic Editor toolbar button |

The script handles both:

```bash
python deploy_plugin.py --dry-run   # show where it will land, write nothing
python deploy_plugin.py             # actually install
```

Expected:

```
source: /path/to/Tracer/tracer_plugin
  plugin.json validates against api.v1.schema.json

IPC API (Schematic + PCB editors)
  -> C:\Users\you\Documents\KiCad\10.0\plugins\tracer_plugin
     + __init__.py
     + plugin.json
     + requirements.txt
     ...

legacy ActionPlugin (PCB editor)
  -> C:\Users\you\Documents\KiCad\10.0\scripting\plugins\tracer_plugin
     + ...
```

`<documents>` is normally `~/Documents/KiCad/10.0`. **If the
`KICAD_DOCUMENTS_HOME` environment variable is set, it is
`$KICAD_DOCUMENTS_HOME/KiCad/10.0` instead** — KiCad silently scans the
redirected location and ignores your Documents folder, which looks exactly like
a broken plugin. `deploy_plugin.py` reads that variable for you, so trust its
`--dry-run` output over this README.

> **`requirements.txt` inside `tracer_plugin/` is mandatory.** KiCad's IPC
> manager builds a private virtualenv per plugin and installs that file into
> it. With the file missing, KiCad creates the venv, then gives up and shows
> **nothing at all** in Preferences ▸ Plugins — no plugin, no error row. Don't
> delete it, and don't add the backend's packages to it.

### Step 5 — Point the plugin at the engine

The plugin now lives in KiCad's folder, nowhere near this repository, so it has
to be told where `agent.py` is. This must **persist**: KiCad is a separate
process started later and will not see a variable you set in one terminal.

```powershell
# Windows -- then restart KiCad
[Environment]::SetEnvironmentVariable('TRACER_HOME','C:\path\to\Tracer','User')
```
```bash
# macOS / Linux -- then restart KiCad
echo 'export TRACER_HOME=/path/to/Tracer' >> ~/.bashrc   # or ~/.zshrc
```

Use the real path to *your* clone. Verify it in a **new** terminal:

```powershell
echo $env:TRACER_HOME        # PowerShell
```
```bash
echo $TRACER_HOME            # bash / zsh
```

### Step 6 — Start the backend

The backend does the placement, routing, verification and ERC. It must be
running whenever you use the plugin, so give it its own terminal:

```bash
# repository root, venv active
uvicorn app:app --reload --port 8000
```

```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Check it from another terminal:

```bash
curl http://127.0.0.1:8000/health
```

```json
{"status":"healthy","engine_version":"0.1.0","kicad_sch_api_version":"0.5.x",
 "erc_available":true,"output_dir":"/path/to/Tracer/generated"}
```

`"erc_available": true` means Tracer found `kicad-cli` and will run KiCad's own
ERC on every result. If it says `false`, Tracer still works — you just lose
that check; set `KICAD_CLI` to your `kicad-cli` path to fix it.

Interactive API docs: <http://127.0.0.1:8000/docs>

### Step 7 — Pre-flight check

Run all four before opening KiCad. Each is independent, so a failure tells you
exactly which piece is wrong.

```bash
python agent.py --check-key                            # 1. key reaches Gemini
curl http://127.0.0.1:8000/health                      # 2. backend is up
python generator.py mock_ai_output.json out.kicad_sch   # 3. engine works, no key needed
python deploy_plugin.py --dry-run                       # 4. plugin is installed
```

Check 3 is the most informative — it exercises the whole layout engine offline.
The last lines should read:

```
  verified: 30 components, 39 wire legs -- all on the 1.27 mm grid, all orthogonal, 27/27 pins connected
  verified: no shorts -- 8 nets' copper checked against every pin (240 pin/net pairs)
  verified: KiCad ERC reports 0 violations at every severity
Wrote out.kicad_sch
```

Open `out.kicad_sch` in KiCad to see what Tracer produces, then delete it —
it's gitignored scratch.

For check 4, every line should read `=` (already up to date), `u` (updated) or
`+` (new). If it prints paths you don't recognise, that's
`KICAD_DOCUMENTS_HOME` at work.

---

## Using it in KiCad

Restart KiCad after installing the plugin.

### Opening the panel

**PCB Editor** — the reliable path, works today:

> *Tools ▸ External Plugins ▸ Tracer Copilot*

**Schematic Editor** — KiCad 10's IPC API:

> *Preferences ▸ Plugins* → click **Refresh Plugins** → **Tracer Copilot**
> appears in the table → use its toolbar button

If that table is empty, see
[the Plugins table is empty](#the-plugins-table-in-preferences-is-empty).

### The panel

A floating window titled **Tracer Copilot**, about 760×660:

```
┌─ Tracer Copilot ─────────────────────────────────────────┐
│ Describe the circuit you want                            │
│ ┌──────────────────────────────────────────────────────┐ │
│ │ (type your prompt here, multi-line)                  │ │
│ └──────────────────────────────────────────────────────┘ │
│ Inject into: [ /path/to/your.kicad_sch ]  [ Browse... ]  │
│ [x] Inject into the sheet above (otherwise leave a       │
│     standalone file)                                     │
│              [ Generate ]  [ Cancel ]                    │
│ Log                                                      │
│ ┌──────────────────────────────────────────────────────┐ │
│ │   engine home : /path/to/Tracer                      │ │
│ │   agent       : /path/to/Tracer/agent.py             │ │
│ └──────────────────────────────────────────────────────┘ │
│ Ready.                                                   │
└──────────────────────────────────────────────────────────┘
```

| Control | What it does |
|---|---|
| **Prompt box** | Your circuit description. Plain English, multi-line. |
| **Inject into** | The `.kicad_sch` the result is merged into. Pre-fills with the sheet you have open. |
| **Browse...** | Pick a different target sheet. |
| **Inject checkbox** | On: merge into the sheet above. Off: write a standalone file into `generated/` and leave your sheet untouched. |
| **Generate** | Start. Runs on a worker thread, so KiCad stays responsive. |
| **Cancel** | Abandon the run in progress. |
| **Log** | Live progress: placement, routing, verification, ERC. |
| **Status** | `Ready.`, or what it is doing now. |

**Read the two startup lines before your first run.** If `agent` says
`NOT FOUND -- set TRACER_HOME`, go back to
[Step 5](#step-5--point-the-plugin-at-the-engine).

### What happens when you press Generate

1. The prompt goes to `agent.py` in your system Python — a subprocess, because
   KiCad's own Python has no Gemini SDK.
2. Gemini returns either a circuit or **one** clarifying question. A question
   pops a dialog; your answer resumes the same conversation with its history
   intact.
3. The circuit is POSTed to the backend, which places, routes, verifies and
   runs ERC.
4. If the router cannot find a clean path, the rejection goes back to Gemini for
   one repair round, then retries.
5. On success, the block is merged into your target sheet.

**Reload the schematic in KiCad afterwards.** Tracer edits the file on disk and
KiCad will not notice by itself — use *File ▸ Revert*, or close and reopen the
sheet.

> **Save your work before injecting.** Injection rewrites the `.kicad_sch`
> file. For your first few runs, either untick the inject checkbox or point
> **Inject into** at a scratch project.

---

## Your first circuit

Make a throwaway project so nothing of yours is at risk:

1. KiCad → *File ▸ New Project*, name it `tracer_test`.
2. Open its Schematic Editor, then its PCB Editor.
3. From the **PCB Editor**: *Tools ▸ External Plugins ▸ Tracer Copilot*.
4. Confirm **Inject into** points at `tracer_test.kicad_sch`.
5. Paste this prompt:

> Build an ATtiny85 board powered from a 2-pin 5V header through an AMS1117-3.3
> regulator. Decouple the regulator input and output, decouple the MCU supply,
> add a 10k pull-up on RESET, and drive two indicator LEDs from PB0 and PB1
> through 330 ohm series resistors.

6. Press **Generate** and watch the log. Roughly:

```
  running python.exe agent.py --json
  | [agent] asking Gemini...
  | [agent] circuit: ATtiny85 Dual LED Blinker (12 components, 8 nets)
  | placing components:
  |     J1   col  0 row   0  @ ( 119.38,  105.41)
  |     U2   col  1 row   0  @ ( 148.59,  105.41)
  |     U1   col  2 row   0  @ ( 185.42,  105.41)  <- controller (sheet centre)
  | routing nets:
  |     +3V3       5 pins -> label-only rail
  |     LED1_CTRL  2 pins -> 4 legs, 0 junctions
  | verified: 30 components, 39 wire legs -- all on the 1.27 mm grid, all orthogonal
  | verified: no shorts -- 8 nets' copper checked against every pin
  | verified: KiCad ERC reports 0 violations at every severity

Injecting into tracer_test.kicad_sch ...
  merged 12 components, 39 wires, 5 labels
Ready.
```

7. Switch to the Schematic Editor and reload the sheet (*File ▸ Revert*).

You should see a left-to-right layout: the header, the regulator with its caps
above and below, the ATtiny centred, resistors and LEDs to the right, and power
rails drawn as labels rather than wires.

Generation usually takes 10–40 seconds, most of it waiting on Gemini.

---

## Writing prompts that work

Tracer is strongest on small, conventional blocks. Name the parts you care
about and say how things connect.

**Works well:**

- *"a 555 astable blinking an LED at about 1 Hz from 5V"*
- *"an ESP32-WROOM-32 with a 3.3V AMS1117 regulator, 100nF decoupling on each supply pin, and a boot button on GPIO0"*
- *"four 10k pull-ups on an I2C bus with a 2-pin header"*
- *"an LM358 non-inverting amplifier with a gain of 10, powered from ±5V"*

**Tips:**

- **Say the supply voltage.** Otherwise Tracer has to ask, costing a round trip
  — and one of your 20 daily requests.
- **Keep it under about a dozen components.** Past that the columns run off an
  A4 sheet and the build fails. Split big designs across several prompts.
- **Name real parts** where it matters (`ATtiny85`, `AMS1117-3.3`, `NE555`). Say
  "a sensor" and Tracer will stop and ask which one.
- **Don't ask for layout.** Position, rotation and spacing are the engine's job;
  asking for them in the prompt does nothing.
- **Don't ask for a PCB.** Tracer generates schematics only.

If Gemini asks a question, answer in a few words. Your answer and the original
prompt are both kept, so you needn't restate anything.

---

## Troubleshooting

### The plugin isn't in the PCB Editor's Tools menu

Wrong directory, or KiCad wasn't restarted.

```bash
python deploy_plugin.py --dry-run
```

Compare the printed path with where you actually copied things — and remember
`KICAD_DOCUMENTS_HOME` overrides the Documents folder. Then restart KiCad, or
use *Tools ▸ External Plugins ▸ Refresh Plugins*.

### The Plugins table in Preferences is empty

No error icon, no row, nothing. Two causes, likeliest first:

1. **`tracer_plugin/requirements.txt` is missing.** KiCad builds the plugin's
   virtualenv, then refuses to mark a Python plugin ready without that file —
   and reports nothing in the table when it bails. Re-run
   `python deploy_plugin.py`.
2. **Wrong directory.** IPC plugins go in `<documents>/plugins/`, *not*
   `<documents>/scripting/plugins/`. The two scanners are unrelated.

Also confirm the API is enabled: *Preferences ▸ Plugins* should say it is
listening on a socket. If it says the API is disabled, enable it and restart.

### "Could not find agent.py"

`TRACER_HOME` is unset or stale. See
[Step 5](#step-5--point-the-plugin-at-the-engine), then restart KiCad — a
variable set after KiCad launched won't reach it.

### "does not appear to be a KiCad project file"

A native KiCad dialog naming a `.py` file. Something handed a script path to a
KiCad executable instead of a Python interpreter. Point `TRACER_PYTHON` at a
real Python that has the Gemini SDK:

```powershell
[Environment]::SetEnvironmentVariable('TRACER_PYTHON','C:\Python312\python.exe','User')
```

### "No Python interpreter with the Gemini SDK was found"

The plugin probes for a Python that can `import google.generativeai`. If your
only copy is inside `venv`, the probe won't find it. Either install it where the
probe looks:

```bash
pip install --user google-generativeai
```

or point `TRACER_PYTHON` at your venv's interpreter, e.g.
`C:\path\to\Tracer\venv\Scripts\python.exe`.

### "The agent did not answer within 300s", or connection refused

The backend isn't running. Start it ([Step 6](#step-6--start-the-backend)) and
check `curl http://127.0.0.1:8000/health`.

### "No Gemini API key found"

Run `python agent.py --check-key` — it reports which file or variable it looked
at. See [Step 3](#step-3--configure-your-gemini-api-key).

### "Gemini rate limit reached"

Free tier, roughly 20 requests per model per day; it resets tomorrow. To keep
going now, change `MODEL_NAME` near the top of `agent.py` — a different model
has its own quota.

### "BACKEND REJECTED THE CIRCUIT — stage: verification"

The router could not find a clean path. The automatic repair round fixes most of
these. If it persists, the layout is too cramped: ask for fewer parts, or raise
`channel_mm` / `body_margin_mm` in the request's `layout` block.

### The schematic looks unchanged after injection

KiCad doesn't notice on-disk edits. Reload the sheet: *File ▸ Revert*, or close
and reopen it.

### Nothing happens and the log is empty

Open the panel from the PCB Editor and read its first two lines (`engine home`,
`agent`). If `agent` says `NOT FOUND`, it's `TRACER_HOME`.

---

## Using the engine without KiCad

Useful for testing, and for seeing what Tracer produces without touching a real
project.

```bash
# Build the sample circuit from JSON. No API key needed -- the engine alone.
python generator.py mock_ai_output.json out.kicad_sch

# Drive the agent from a terminal. Needs a key, and the backend running.
python agent.py "a 555 astable blinking an LED at 1Hz"

# One JSON object in, one out -- the exact interface the plugin uses.
echo '{"prompt":"a 10k pull-up to 3V3","history":[],"post":false}' \
  | python agent.py --json
```

`mock_ai_output.json` also documents the full circuit JSON schema, which is
handy if you want to hand-write a block and skip the LLM entirely.

---

## How it works inside

Four pieces, deliberately separate:

**`agent.py`** — talks to Gemini. Sends a system prompt that pins the JSON
schema down exactly, because the backend rejects unknown keys outright. Handles
clarification rounds and one layout-repair round. Runs in *your* Python, not
KiCad's, because KiCad's has no Gemini SDK. Reads the key from the environment
or `.env`.

**`app.py`** — a small FastAPI service. Validates the block with Pydantic
(`extra="forbid"`, so a hallucinated field is a hard 422 rather than something
the engine silently ignores), then calls the engine and writes the file.

**`generator.py`** — the layout engine, and the bulk of the code. Places
components into left-to-right columns from their `(column, row)` slots with the
controller pinned to the sheet centre; routes each signal net with orthogonal
legs that must clear every other pin, every other net's copper, and every
symbol's drawn body; adds junctions and labels; places property text so nothing
overlaps; then verifies the whole sheet and runs KiCad's ERC. Anything it cannot
do cleanly is an error, not a warning.

**`tracer_plugin/`** — the KiCad side. One wxPython panel shared by both front
doors, a worker thread so KiCad never blocks, and `tracer_inject.py` to merge a
generated block into an existing sheet.

The split is the point: the LLM handles *intent*, the engine handles *geometry*,
and neither is trusted with the other's job.

---

## Repository layout

```
Tracer/
├── agent.py             prompt ▸ Gemini ▸ circuit JSON; clarification and
│                        repair rounds; --check-key
├── app.py               FastAPI service wrapping the engine
├── generator.py         the layout engine: place, route, verify, ERC
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

Not in the repository, created as you go: `.env` (your key), `venv/`, and
`generated/` (engine output).

---

## Known limitations

- **Schematics only.** No PCB layout, no copper routing.
- **About a dozen components on A4.** Past that the columns run off the sheet
  and the build fails outright. Split the design.
- **Free-tier quota**, roughly 20 requests per model per day.
- **You must reload the sheet in KiCad** after injection; Tracer edits the file
  on disk and KiCad doesn't watch for that.
- `kipy.schematic` cannot be imported in `kicad-python` 0.8.0 (an upstream
  `PageSettings` bug), so Tracer edits `.kicad_sch` files directly rather than
  through the IPC API. It only asks the API *which* file is open.
- KiCad 10.0.5's IPC API doesn't return the open schematic's absolute path, so
  Tracer resolves it from the Schematic Editor's recent-file list. If the
  pre-filled target is ever wrong, use **Browse**.
- `agent.py` targets Google's legacy `google-generativeai` SDK, deprecated
  upstream in favour of `google-genai`. Migration is not done yet.
- Tested on Windows 11 with KiCad 10.0.5. The macOS and Linux code paths exist
  but have had far less exercise.

---

## License

GPL-3.0-or-later, matching KiCad. See `metadata.json`.
