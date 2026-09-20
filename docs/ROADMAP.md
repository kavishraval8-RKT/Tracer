# Tracer — Architecture Roadmap

> **What already shipped is in [CHANGELOG.md](CHANGELOG.md).** This file
> covers what's *next*. The "Slice A–J" names that appear elsewhere in
> discussion or older commit messages map to the SaaS phases below —
> the mapping table in the CHANGELOG is the reconciliation.


Where the code is, what to build next, in what order, and why. Living
document — edit as decisions land. Companion to [SETUP.md](../SETUP.md)
(usage) and [DEFERRED.md](DEFERRED.md) (scoped-but-not-built items).

Last updated: 2026-09-14.

---

## Current state (2026-09-14)

**Working end-to-end on Windows 11 + KiCad 10 + Python 3.14:**

- KiCad 10 IPC plugin (`tracer_plugin/`) spawns a detached PySide6 sidecar
  (`sidecar_app.py`) via `sidecar_launcher.py`. No in-process wxPython.
- Two frameless translucent panels: dock (bottom pill, `assets/dock.html`)
  and chat (right rail, `assets/chat.html`). QWebEngineView + QWebChannel.
- Auto-target resolution across sch/pcb window titles + KiCad's own
  `system.file_history` in `eeschema.json`/`pcbnew.json`/`kicad.json`.
- Four modes with distinct routing:
  - **Agent** — full generate + backend build + optional auto-inject
  - **Ask** — plain-text Q&A via `agent.ask_gemini` (no backend, no inject)
  - **Plan** — design outline via `agent.plan_gemini` (no backend, no inject)
  - **Debug** — Agent + verbose Activity dump
- **Clarification-aware routing**: while Agent has a pending clarification,
  the next Send is pinned to the Agent worker regardless of the mode
  dropdown state (dropdown greys out, send button turns amber).
- Undo ledger with timestamped `.tracer-bak.<YYYYMMDD_HHMMSS>` snapshots per
  successful inject. Modal opens in the chat panel from the dock button.
- Post-inject `kicad-cli pcb drc --schematic-parity` streams violations and
  parity mismatches into the Activity log.
- Custom dropdowns for mode/model that render inside the QWebEngineView
  without geometry churn (permanent 300px dock height + `QRegion` input
  mask on the pill so KiCad receives clicks in the transparent top area).

**Known limits:**

- Windows-only. Uses `pywin32` for window discovery + `keybd_event` for
  Ctrl+F5 reload.
- `tracer_inject.py` writes `.kicad_sch` by text-block manipulation.
  Version-fragile (arch doc's original warning still stands).
- No sync automation. User has to hit F8 in KiCad PCB Editor after inject.
- No autoroute — deferred with reopen triggers in DEFERRED.md.
- Test coverage: one 15-line focus-window test. Nothing else.
- `_history` grows unbounded and is shipped to Gemini every turn.

---

## Phase 2 — near-term (the next 2–4 weeks of work)

Order below is intentional. Each item depends on the ones above it landing
first.

### 2.1 Test harness (blocks everything below)

**Why first:** every subsequent item touches parsers, workers, or state
machines that landed this month with zero regression coverage. Refactoring
without tests is how bugs get introduced silently.

**Scope:**

- `tests/` dir at repo root. `pytest`, no fixtures framework needed.
- One test file per module under test. Fixtures = plain `conftest.py` +
  files under `tests/fixtures/`.
- Load-bearing units to cover (in priority order):
  - `agent._project_from_title` — window-title parser (em-dash / en-dash /
    hyphen, with and without leading `*`)
  - `KiCadWatcher.find_open_schematic` (mocked EnumWindows) —
    schematic-editor hit, PCB-editor fallback, project-name synth,
    `.kicad_sch` extension recovery
  - `KiCadWatcher.find_open_schematic` resolver — `eeschema.json` /
    `pcbnew.json` / `kicad.json` `system.file_history` walks
  - `PcbDrcWorker._parse` (extract from `run()` first) — the DRC JSON
    shape from KiCad 10 including `violations`, `unconnected_items`,
    `schematic_parity`
  - `_undo_ledger` snapshot + restore round-trip on a tempfile pair
  - `_project_from_title` + `_pending_clarification` state transitions

**Anti-goals:**

- No headless PySide6 event-loop tests. Qt fixtures are fragile and slow.
- No Gemini calls in tests. Mock `agent.query_gemini` / `agent.ask_gemini`.
- No KiCad process tests. Mock `win32gui`.

**Exit criterion:** `pytest -q` finishes in under 5 seconds, covers every
function named above, and fails when any of the last month's fixes is
reverted.

### 2.2 Split `sidecar_app.py`

**Why after tests:** it's 1550+ lines. Every developer touching it right now
scrolls for context. Refactoring without tests is high-risk.

**Proposed layout** (single-directory package, no over-nesting):

```
sidecar/
├── __init__.py
├── main.py           # entrypoint + QApplication + instance mutex
├── shell.py          # OverlayShell — the state machine
├── windows.py        # OverlayWindow, geometry logic, mask
├── bridge.py         # PyBridge — all @Slot methods, signals
├── workers.py        # GenerateWorker, AskWorker, InjectWorker, PcbDrcWorker
├── watcher.py        # KiCadWatcher, foreground / focus polling
├── snapshots.py      # Undo ledger + tracer-bak file management
├── platform_win.py   # pywin32 imports + Windows-specific helpers
└── settings.py       # env vars, config constants (paths, ports, intervals)
```

`sidecar_app.py` becomes a 5-line shim: `from sidecar.main import main; main()`.
Preserves the launcher's existing target path.

Same treatment for `agent.py` (57 KB) is out of scope for this step —
tackle only if agent.py continues to grow.

**Exit criterion:** every test from 2.1 still passes; `sidecar_app.py`
shrinks to under 20 lines; no module in the new `sidecar/` package exceeds
400 lines.

### 2.3 AST-based inject engine (`tracer_ast/`)

**The reliability bet.** `tracer_inject.py` currently does text-block
manipulation of `.kicad_sch`. KiCad 10.1's format changes will silently
break it. The original architecture doc called this "the reliability-
critical file-writing internals" and gated user trust on it.

**Current state:** `tracer_ast/sexpr_parser.py` (254 lines) is a stub.
Nothing consumes it.

**Scope in dependency order:**

- **Parser + typed model** (`sexpr_parser.py` finish + `nodes.py`):
  parse `.kicad_sch` s-expressions into a typed tree that survives
  round-trip. Every node type used by KiCad 10 (`symbol`, `wire`,
  `junction`, `label`, `text`, `sheet`, `title_block`, `lib_symbols`,
  numeric coordinates preserving formatting) modeled explicitly.
- **UUID registry** (`uuids.py`): every inject-created node gets a fresh
  UUID; injecting the same block twice produces distinct UUIDs; existing
  UUIDs are never rewritten. Guard rail against clashes.
- **Mutators** (`mutators.py`): `add_symbol`, `add_wire`, `add_label`,
  `move_symbol`. All operate on the typed model; no string surgery.
- **Serializer** (`writer.py`): typed model → bytes. Round-trip identity:
  `parse(read(f)) → write(x) → parse(read(x)) == parse(read(f))` when
  no mutations happen.
- **Placement engine** (`placement.py`): bounding-box + clearance solver
  for where injected components land relative to existing content.
  Uses the parsed model to know what's already there.
- **Wire `generator.py` and `tracer_inject.py` to call mutators
  instead of text templates.** Old `tracer_inject.py` becomes a thin
  compatibility shim → deprecate → remove in phase 3.

**Anti-goals for this phase:**

- No schematic hierarchy support (sheets that reference other sheets).
  Single-sheet only until phase 3.
- No PCB writer. `.kicad_pcb` still updated via KiCad's F8 (or the phase-3
  IPC bridge). We don't own PCB mutation.

**Exit criterion:** round-trip fidelity harness (below) runs a corpus of
real `.kicad_sch` files through `parse → serialize` and every byte matches
the input.

### 2.4 Round-trip fidelity harness

**Why standalone item:** a hard checkpoint that gates trusting the AST
work. Small runnable script + a corpus of `.kicad_sch` files.

**Scope:**

- `tests/fidelity/corpus/` — checked-in `.kicad_sch` files representing
  the shapes we care about (empty sheet, single symbol, hierarchical
  labels, junctions, buses, text blocks, edge cases).
- `tests/fidelity/test_round_trip.py` — for each file:
  1. Read bytes.
  2. Parse.
  3. Serialize.
  4. Byte-diff against original. Any unexpected difference fails.
- Documented in [`tests/fidelity/README.md`](../tests/fidelity/README.md) —
  how to add a new fixture when a KiCad edge case surfaces.

**Exit criterion:** ≥10 real `.kicad_sch` files round-trip cleanly. Every
mutator has at least one before/after test.

### 2.5 History cap (small but real)

**Why now:** `_history` is unbounded and shipped in full every turn.
Gemini charges by input tokens. A user with a 40-turn conversation is
paying for 40 turns worth of context per new turn.

**Scope:**

- Cap `_history` at N turns (start at 12) before shipping.
- Older turns get summarized (single LLM call at cap boundary) into one
  compact "prior context" system message that carries forward.
- Config: `TRACER_HISTORY_CAP` env var override.

**Exit criterion:** long conversation stays under a predictable token
budget; summarization only fires when actually needed.

---

## Phase 3 — mid-term (after phase 2 lands)

Ordered from most-valuable to least.

### 3.1 Multi-turn iterative editing

**The Phase-3 vision from the original arch doc.** "Change R2 to 10k"
should mutate the existing pending plan, not regenerate from scratch.

**Requires phase 2.3** because delta mutations need the AST — text-template
inject can't do surgical edits reliably.

**Scope sketch:**

- `PendingPlan` object carried across turns in `agent.py`.
- Agent prompt shape includes the current plan state (component list,
  net list, target coordinates).
- Detection of edit vs. new-request intent (probably by heuristic + one
  LLM call).
- Delta mutators: `edit_symbol_value`, `remove_symbol`, `rename_net`,
  `move_symbol`.

**Anti-goal:** no visual plan-diff yet. Text-only iteration for phase 3.1.

### 3.2 Plan-review card UI

Turn Plan mode's text output into a structured card (component table,
net table, notes) with per-component approve/reject checkboxes. Feeds into
Agent mode as a filtered plan.

**Requires 3.1** (needs the `PendingPlan` object) and 2.3 (needs the AST
to know what "approve component" means structurally).

### 3.3 Freerouting bridge (see DEFERRED.md)

Only reopen when:
- Boards routinely hit >50 nets, AND
- Manual routing becomes a repeated pain point.

Both conditions must hold. `kicad-python`'s `run_action` is explicitly
marked unstable — buying into it costs ongoing maintenance every KiCad
release.

### 3.4 Real sch→pcb sync

`kicad-cli` can't do it. Requires `kicad-python` IPC + `run_action` (same
unstable API surface as Freerouting). Ships the "click Send → PCB updates"
loop without needing F8.

**Prerequisite:** the Freerouting spike (3.3) validates that `run_action`
actually works headlessly. If that spike fails, 3.4 is dead too.

---

## SaaS distribution track (parallel to Phase 2/3)

Announced late in-session; not part of the original phase 2 sequence but
now the primary next-work track. Cloud-only (BYOK dropped). Streaming =
progress events only. Every phase below stops at a shippable state.

- [x] **SaaS-1 — Backend scaffold**: FastAPI + async SQLAlchemy + Alembic;
  local SQLite dev; models for User, Subscription, DeviceCode,
  UsageLedger; `/health` + `/version` endpoints; monorepo `backend/`
  folder.
- [x] **SaaS-2 — Device Code Flow**: RFC 8628 endpoints
  (`/device/code`, `/device/token`), JWT issuance with `jti`
  rotation for single-plugin-per-user enforcement, `/activate` page
  frontend, plugin Login button + polling loop, JWT storage in
  `%APPDATA%\Tracer\token.json`.
- [x] **SaaS-3 — LLM proxy + quota** (SSE deferred). `/v1/generate|ask|plan|me`
  gated by the JWT dependency; plugin routes via `tracer_api.py`.
  Every request runs `quota.check_quota` before the LLM call and
  writes a `usage_ledger` row after — 402 when over the tier budget.
  New `GET /v1/usage` returns `{used_tokens, quota_tokens,
  remaining_tokens}` for the dashboard. Token counting is estimated
  (`len//4`); metadata-driven counting deferred until agent.py's
  return shape changes for other reasons. SSE progress events still
  pending.
- [ ] **SaaS-4 — Deploy**: Render service, health probes on cold start,
  plugin cloud-down error UX (no BYOK fallback — hard fail with clear
  message).
- [ ] **SaaS-5 — Stripe subscription**: tier definitions
  (free/pro/team), Stripe checkout, webhook flips tier + resets quota,
  usage dashboard on website.

---

## Phase 4 — long-term (worth naming, not scheduling)

### 4.1 Cross-platform

Windows-only right now. `pywin32`, `keybd_event`, hardcoded
`C:\Program Files\KiCad\...` paths. Every platform check is a call site
we need to abstract:

- Window discovery: swap `win32gui.EnumWindows` for a platform-mux
  (Windows keeps pywin32; macOS uses Quartz; Linux uses X11/Wayland
  querying).
- Reload trigger: `send_reload_to_kicad` becomes `platform.reload_kicad()`.
- KiCad path resolution: use `shutil.which` first, `Program Files` /
  `/Applications/KiCad.app/Contents/MacOS/` / `/usr/bin/` fallbacks.
- Deployment: `deploy.py` reads `KICAD_DOCUMENTS_HOME`, or resolves per-
  platform defaults.

Non-goal for this phase: writing Linux packaging (.deb, .rpm). Ship as
"clone + `pip install`" on Linux, same as Windows.

### 4.2 Packaging + distribution

PyInstaller or Nuitka bundle. `kicad-cli` and `pywin32` resolution at
runtime. Signed installer on Windows, notarized DMG on macOS.

Revisit the Tauri question the original arch doc flagged. Only if
PySide6/QWebEngineView bundle size becomes a real user complaint.

### 4.3 Persistent state

Undo ledger currently lives in memory only. Restart wipes it while the
`.tracer-bak.*` files linger orphaned on disk. Persist the ledger to a
`.tracer-state.json` next to the project.

Model preference (forced model) should also persist per-project.

---

## Cross-cutting concerns

Not owned by any single phase, applies to all work below:

- **No mocks in integration tests.** The one lesson from the wxPython
  legacy — mocked KiCad tests passed while the real path failed. Any
  end-to-end verification uses real `.kicad_sch` files.
- **KiCad version churn.** Assumptions we make about JSON schemas
  (`system.file_history`), CLI subcommand names, and IPC action IDs are
  ALL unstable across KiCad releases. Isolate them behind a
  `kicad_probe.py` that logs a warning when a probe fails, doesn't crash.
- **Error handling austerity.** No blanket `except Exception:` swallow.
  Every silent error is a bug we can't diagnose later. When we catch, we
  log a specific line to Activity or stderr with enough context to
  bisect.
- **Bridge slot budget.** `PyBridge` has ~15 slots now. Adding one costs
  touching sidecar + dock/chat HTML + Python. Cap at 25 total before
  considering a message-oriented alternative (single `dispatch(name,
  args)` slot with an internal router).

---

## Anti-goals (worth naming so we don't drift)

- No frontend framework (React/Vite/TS). Tailwind CDN + QWebChannel
  works. Framework adds build step, node_modules, and TS overhead to a
  KiCad plugin distribution. Rejected earlier in this repo's history.
- No autoroute until DEFERRED.md's triggers hit.
- No LLM-as-router. The literature on LLMs for EDA routing is clear:
  they crater on this task.
- No non-KiCad EDA support. Scope is one tool, done well.

---

## How to update this doc

- Landing a phase item: strike through the item, add the commit sha next
  to it. Don't delete until the whole phase closes.
- Discovering new blockers: add to the item's scope, don't create sibling
  items.
- Deferring: move to DEFERRED.md with a **trigger to reopen** line, not
  just a date.
- Reordering: allowed, but explain the dependency change in the commit
  message.
