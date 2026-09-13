# Tracer Floating Sidecar — Architectural Blueprint, UX Spec & Roadmap

**Author role:** Principal Desktop Systems Architect / EDA Tools Engineer
**Scope:** Pivot Tracer from an in-process KiCad plugin to a standalone floating desktop companion.

---

## 0. Framing the Pivot

The core architectural shift is from **in-process plugin** (constrained by Eeschema's Python API surface, which KiCad 10 does not expose for docking/toolbar/live-canvas hooks) to **out-of-process companion app** that treats KiCad as an external, opaque process it observes and occasionally writes to. This is the same trust model Copilot/Cursor use toward VS Code before LSP-level integration existed, or toward any editor without a plugin API at all.

Three consequences fall out of this immediately, and they shape every section below:

1. **No live canvas access for schematics** → KiCad's IPC API (protobuf/nng, stabilizing since KiCad 9) already supports genuinely live, no-reload editing of the **PCB editor** — a client can command a running KiCad instance directly and it repaints instantly, integrated with KiCad's own undo/redo. But as of KiCad 10, that live channel has **not been extended to the schematic editor (Eeschema)** — KiCad's own developers list this as future work, not something in the current release. So for schematics specifically, Tracer can only write files and *ask* the user (or use OS-level automation, discussed later) to reload. This is a hard platform limit today, not a design shortcut on our part — worth revisiting the mutation layer's backend if/when KiCad ships schematic-editor IPC support, which is why §3.1's layers are kept separate from the file-write mechanics.
2. **No reliable "active project" signal from KiCad itself** → Tracer must infer context from the OS (window titles, recent-files, filesystem watching) with manual override as the trust fallback.
3. **File writes must be surgical and reversible** → since Tracer is now "a stranger editing your file from outside," correctness and rollback guarantees matter more than they did when it was an in-process plugin the user explicitly invoked.

---

## 1. Framework & UI Architecture

### 1.1 GUI Stack Evaluation

| Criterion | PySide6 (Qt) | wxPython | Webview (pywebview/Tauri/Electron) |
|---|---|---|---|
| Native dark theme fidelity to KiCad (which is itself Qt-based, wxWidgets-legacy-free since KiCad 6+) | **Excellent** — KiCad 6+ uses Qt-styled widgets internally via its own wx→Qt-like theming; PySide6 gives you native QSS control to closely mimic KiCad's dark palette | Weaker — wx theming is platform-native by default and fighting it for a custom dark mode is more work | Excellent — full CSS control, easiest to pixel-match any theme |
| Always-on-top / frameless / snap-to-edge window management | Strong, mature `Qt.WindowStaysOnTopHint`, `Qt.FramelessWindowHint`, `QScreen` geometry APIs for edge-snapping | Supported but more boilerplate (`wx.STAY_ON_TOP`, manual frameless handling is clunkier) | Depends on shell: Tauri (Rust shell) has good native window control; Electron has it but at heavy resource cost; pywebview's window controls are the thinnest of the three |
| Streaming chat UI w/ code/circuit preview cards | Native widgets require custom-built rich-text/streaming widgets (doable with `QTextEdit` + custom delegate, or embed a `QWebEngineView` for rich content) | Same problem, fewer community examples | **Best fit** — chat UIs are fundamentally HTML/CSS/JS problems; streaming markdown, syntax highlighting, and animated diff cards are trivial with existing JS libraries (e.g., `react-markdown`, `shiki`) |
| Packaging size / resource footprint | Small (~40–80 MB installed), fast startup | Small, fast startup | Tauri: small (~10–20 MB, uses OS webview); pywebview: small; **Electron: large (100–200MB+), heavier RAM** |
| Python-native backend integration (agent.py, generator.py already in Python) | Direct, in-process import possible | Direct, in-process import possible | pywebview: direct (Python backend, JS frontend, bridged); Tauri: requires IPC to a Python sidecar process (Rust core, Python as subprocess) — adds a hop; Electron: same subprocess requirement, in Node |
| Cross-platform consistency (Win/Mac/Linux, since embedded engineers use all three) | Very good, single codebase | Good, but platform-specific quirks are more common (menu bars, HiDPI issues on Linux/GTK) | Tauri: excellent, uses native OS webview per platform (WebView2/WebKit/WebKitGTK) — slight rendering differences across those three engines; Electron: most consistent because it ships its own Chromium; pywebview: same caveat as Tauri |
| Maturity of "floating companion" precedent | Used for exactly this pattern in various dev tools | Less common for this specific pattern | This is the dominant pattern for AI copilots (Cursor, Copilot Chat panel, Raycast) — most prior art here |

**Recommendation: PySide6 for the shell, with a `QWebEngineView` panel embedded for the chat/streaming/diff-card surface.**

Rationale:
- You get native, fast window management (always-on-top, frameless, edge-snap, collapse-to-tab) from Qt — this is the part that's annoying to get pixel-perfect in a pure webview shell like Tauri/pywebview, especially collapsible "pill" minimization against a screen edge.
- You get modern, flexible chat/diff-card rendering from the embedded Chromium engine (`QWebEngineView`), without paying Electron's full-process tax — `QWebEngineView` shares Qt's process, and you're not shipping a second Chromium runtime the way Electron does.
- Your backend (`agent.py`, `generator.py`, `app.py`) stays pure Python and can run in the **same process** as the UI (as a background thread or `asyncio` loop), avoiding IPC entirely for the common path — see 1.2 below.
- wxPython is not recommended: its "we mimic the free OS theme" philosophy actively fights your goal of a bespoke dark, KiCad-matching aesthetic, and its `QWebEngineView`-equivalent (`wx.html2`) is a worse-supported wrapper (WebView2/WebKit binding quality varies more).
- Full Electron is not recommended: your backend is already Python, so Electron forces a Python subprocess + a second heavy runtime for zero UI benefit over PySide6+QWebEngineView.
- Tauri is the strongest *runner-up* if you want to eventually ship this as a wholly separate, lighter binary decoupled from Python packaging (PyInstaller/Nuitka) concerns — but it requires bundling and lifecycle-managing a Python sidecar subprocess, which reintroduces the IPC boundary you'd otherwise avoid. Worth revisiting for Phase 3+ if PySide6 packaging size becomes a complaint.

### 1.2 Process Model

Two realistic options:

**Option A — Embedded (recommended for Phase 1–2):**
FastAPI (`app.py`) runs `uvicorn` in a background thread inside the same Python process as the PySide6 UI. The `QWebEngineView` chat panel talks to it over `http://127.0.0.1:<port>` exactly as a real web client would — this means your existing FastAPI contract, request/response schemas, and streaming (SSE or websockets) endpoints are reused *unmodified*. Zero new protocol work.

```python
# sidecar_app.py
import threading, uvicorn
from PySide6.QtWidgets import QApplication
from app import app as fastapi_app   # your existing FastAPI instance

def run_backend():
    uvicorn.run(fastapi_app, host="127.0.0.1", port=8731, log_level="warning")

if __name__ == "__main__":
    backend_thread = threading.Thread(target=run_backend, daemon=True)
    backend_thread.start()

    qt_app = QApplication([])
    from ui.main_window import SidecarWindow
    window = SidecarWindow(backend_url="http://127.0.0.1:8731")
    window.show()
    qt_app.exec()
```

Trade-off: the FastAPI process and the UI process share fate — if the LLM agent hangs or crashes the interpreter, the whole app goes down. Mitigate by keeping `generator.py`'s heavier geometry/validation work off the Qt event loop thread (it already lives behind FastAPI's async workers, so this is mostly already true) and by wrapping the uvicorn thread with a watchdog that can restart it without killing the UI.

**Option B — Persistent local daemon (recommended once you have a CLI/menu-bar tray icon or multiple front-ends):**
Run `app.py`'s FastAPI server as a standalone, independently-launched local service (e.g., installed as an OS service or launched at login), and have the PySide6 sidecar be a pure client over HTTP/WebSocket to `127.0.0.1`. This decouples UI restarts from backend/session state (useful once you have multi-turn conversation state you don't want to lose when the user closes the floating panel) and lets you later add a second front-end (e.g., a CLI, or a browser-based fallback) against the same backend.

**Recommendation:** Start with Option A for MVP simplicity (Phase 1), and plan the FastAPI app to be startable either embedded or standalone from day one (i.e., don't hardcode assumptions about being launched by the Qt shell) so that migrating to Option B in Phase 3 is a launch-script change, not a rearchitecture.

### 1.3 Window Management & Aesthetic Notes

- **Frameless + always-on-top:** `Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint`, with a custom title bar (drag region + collapse/pin/close buttons) drawn in Qt to match KiCad's dark chrome.
- **Snap-to-edge:** on `moveEvent`, check window geometry against `QScreen.availableGeometry()` edges within an ~20px threshold and animate-snap; persist last-docked edge in `QSettings` so it restores position on relaunch.
- **Collapsible minimalism:** a "pill" collapsed state (just an icon + drag handle) that expands on click/hover, similar to Copilot's floating suggestion widget — implement as two `QWidget` states swapped via `QStackedWidget`, not a resize animation on the full chat surface (resizing a live `QWebEngineView` on every frame is janky).
- **Dark theme parity:** don't try to introspect KiCad's live theme at runtime (fragile, version-dependent); ship a fixed dark palette matching KiCad's default dark theme swatch (near-black `#1a1a1a` background panels, KiCad's characteristic muted blue/green/orange accent hues for nets/components) as a static QSS + CSS token set shared between the Qt chrome and the embedded web content.

---

## 2. Project Detection & File Watching

Since Eeschema gives you no hooks, "what schematic is the user working on right now" must be inferred. Layer these strategies from most-to-least authoritative, and always let the user override:

### 2.1 Auto-Discovery Strategies

**(a) Window title inspection (highest-confidence, real-time signal)**
KiCad's Eeschema window title includes the open schematic filename (e.g., `"blinky.kicad_sch — Schematic Editor"`). Poll or subscribe to the OS window list:
- Windows: `pywin32` (`EnumWindows` + `GetWindowText`), filter for `"Schematic Editor"` or `.kicad_sch`.
- macOS: `Quartz.CGWindowListCopyWindowInfo`, filter by owning process name `eeschema`.
- Linux (X11): `python-xlib` or shell out to `wmctrl -l`; (Wayland): significantly harder — most compositors don't expose window titles to arbitrary processes for privacy reasons, so **plan for this to silently degrade to strategy (b)/(c) on Wayland** and surface that gracefully rather than erroring.

This is your **primary live signal** — it tells you which file is on-screen *right now*, which matters because a user can have multiple `.kicad_pro` projects open.

**(b) KiCad recent-project history file (reliable fallback, not real-time)**
KiCad maintains a `kicad_common.json` (global config) with a `system.recent_projects` (or equivalent, version-dependent — verify against the specific KiCad 10 config schema) list of recently opened `.kicad_pro` paths, timestamp-ordered. Read this on Tracer startup to pre-populate a "likely project" guess, and re-poll it periodically (it updates on project open/close) as a secondary corroborating signal alongside (a).

**(c) Directory-polling file watcher (`watchdog`) for change detection, not initial discovery**
Once a target project directory is established (via (a), (b), or manual override), use `watchdog` to monitor the `.kicad_pro` project directory for `.kicad_sch` modification events — this is how Tracer notices *external* changes (the user editing manually in KiCad) so it can invalidate any stale in-memory AST cache and re-parse before its next injection, avoiding clobbering the user's concurrent manual edits.

```python
# project_watcher.py
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pathlib import Path

class SchematicChangeHandler(FileSystemEventHandler):
    def __init__(self, on_change_callback):
        self.on_change_callback = on_change_callback

    def on_modified(self, event):
        if event.src_path.endswith(".kicad_sch"):
            self.on_change_callback(Path(event.src_path))

def watch_project(project_dir: Path, callback):
    observer = Observer()
    observer.schedule(SchematicChangeHandler(callback), str(project_dir), recursive=True)
    observer.start()
    return observer
```

**Confidence fusion:** combine (a) as the authoritative "which file is active" signal when available, (b) as the "which project have you touched recently" prior when (a) is unavailable (e.g., Wayland) or ambiguous, and (c) as the continuous integrity check regardless of which of (a)/(b) established the target. Surface the inferred target in the UI at all times (e.g., a small "Editing: `blinky.kicad_sch`" chip) so the user always sees — and can correct — Tracer's belief before it writes anything.

### 2.2 Manual Override Fallback

- **Drag-and-drop target selector:** accept a dropped `.kicad_pro` or `.kicad_sch` file directly onto the floating panel (Qt's `dragEnterEvent`/`dropEvent` on the main window), which pins that exact file as the target and disables auto-discovery until the user drops a new file or explicitly re-enables auto mode.
- **Explicit picker:** a simple "Select schematic…" button opening a native `QFileDialog`, for headless/Wayland/multi-monitor situations where window-title sniffing is unavailable or wrong.
- **Pinning UX rule:** once a user manually selects/drops a target, treat that as a *stronger* signal than auto-discovery until they explicitly clear it — don't let a window-title poll silently override a deliberate user choice.

---

## 3. S-Expression AST Manipulation

Moving from text-block replacement (`tracer_inject.py`'s current approach) to structural manipulation, in the spirit of `psychogenic/kicad-skip`, is the single highest-leverage reliability improvement in this pivot. Text-block replacement is inherently fragile against KiCad's own formatting changes across versions/saves; a proper S-expression AST is not.

### 3.1 Core Abstraction Layers

Design this as four cleanly separated layers, each independently testable:

```
tracer_ast/
├── sexpr_parser.py       # Layer 1: raw text <-> S-expr tree (generic, no KiCad semantics)
├── schematic_model.py    # Layer 2: typed KiCad schema (Symbol, Wire, Label, Net, Sheet, ...)
├── mutators.py           # Layer 3: semantic operations (add_symbol, add_wire, add_label...)
├── placement.py          # Layer 4: geometry/bbox/clearance logic for "where does this go"
├── uuid_registry.py       # cross-cutting: UUID collision guarantees
└── snapshot.py            # cross-cutting: rollback/backup management
```

**Layer 1 — `sexpr_parser.py`:** A minimal recursive-descent S-expression tokenizer/parser producing a generic tree of `SExprAtom | SExprList`. This layer knows nothing about KiCad — it's a reusable Lisp-like data format parser. (You can adopt `kicad-skip`'s parser directly, or `sexpdata`/`sexpr` as a base, but writing ~150 lines by hand gives you full control over round-trip formatting fidelity, which matters — see 3.4.)

**Layer 2 — `schematic_model.py`:** Wraps the raw tree in typed accessor objects mirroring KiCad's schema: `Symbol`, `Wire`, `Junction`, `Label` (local/global/hierarchical), `NoConnect`, `Sheet`, `SheetInstance`. Each wraps a reference to its underlying `SExprList` node so mutations write back into the same tree (no separate "model → re-serialize from scratch" step, which is where formatting fidelity is usually lost).

```python
# schematic_model.py
from dataclasses import dataclass
from sexpr_parser import SExprList

class Symbol:
    def __init__(self, node: SExprList):
        self._node = node  # live reference into the parsed tree

    @property
    def uuid(self) -> str:
        return self._node.get_field("uuid")

    @property
    def position(self) -> tuple[float, float]:
        at = self._node.get_field("at")
        return (float(at[0]), float(at[1]))

    def set_position(self, x: float, y: float):
        self._node.set_field("at", [x, y, self._node.get_field("at")[2]])

class SchematicDocument:
    def __init__(self, tree: SExprList):
        self._tree = tree

    def symbols(self) -> list[Symbol]:
        return [Symbol(n) for n in self._tree.find_all("symbol")]

    def wires(self) -> list["Wire"]:
        return [Wire(n) for n in self._tree.find_all("wire")]
    # ... etc
```

**Layer 3 — `mutators.py`:** The API `generator.py` actually calls. High-level, intention-revealing operations, e.g.:

```python
def add_symbol(doc: SchematicDocument, lib_id: str, position: tuple[float,float],
               reference: str, value: str) -> Symbol:
    uuid = uuid_registry.allocate(doc)
    node = build_symbol_node(lib_id, position, reference, value, uuid)
    doc.tree.append(node)
    return Symbol(node)

def add_wire(doc: SchematicDocument, start: tuple[float,float], end: tuple[float,float]) -> Wire: ...
def add_label(doc: SchematicDocument, text: str, position: tuple[float,float], kind: str = "local") -> Label: ...
def connect_net(doc: SchematicDocument, pin_a: PinRef, pin_b: PinRef) -> None: ...
```

This is the layer `generator.py`'s router/verifier targets instead of raw text templates — it already produces validated geometry/clearance/net decisions, so its job becomes "call these mutators with the decided parameters" rather than "stamp text into a file."

**Layer 4 — `placement.py`:** covered in 3.4 below.

### 3.2 UUID Uniqueness Guarantee

KiCad schematics require globally-unique UUIDs per symbol/wire/junction/sheet-instance; colliding UUIDs cause silent corruption or netlist errors that are painful to debug. Guarantee uniqueness with a registry seeded from the *existing* document, not just "generate a fresh uuid4 and hope":

```python
# uuid_registry.py
import uuid

class UUIDRegistry:
    def __init__(self, existing_doc_tree):
        self._used: set[str] = set(existing_doc_tree.find_all_uuid_strings())

    def allocate(self) -> str:
        while True:
            candidate = str(uuid.uuid4())
            if candidate not in self._used:
                self._used.add(candidate)
                return candidate
```
`uuid4`'s collision probability is already negligible, but scanning the existing document and rejecting on match costs nothing and converts "negligible" into "provably impossible for this run," which matters for an automated tool writing files unattended.

### 3.3 Rollback Snapshots

Every injection operation snapshots before writing, using a timestamped, content-addressed backup directory rather than a single `.bak` file (so multiple undo points survive a session):

```python
# snapshot.py
import shutil, time
from pathlib import Path

class SnapshotManager:
    def __init__(self, project_dir: Path):
        self.backup_dir = project_dir / ".tracer-bak"
        self.backup_dir.mkdir(exist_ok=True)

    def snapshot(self, sch_path: Path) -> Path:
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = self.backup_dir / f"{sch_path.stem}.{ts}.kicad_sch.bak"
        shutil.copy2(sch_path, dest)
        self._prune(sch_path.stem, keep=20)
        return dest

    def _prune(self, stem: str, keep: int):
        backups = sorted(self.backup_dir.glob(f"{stem}.*.bak"))
        for stale in backups[:-keep]:
            stale.unlink()

    def restore(self, backup_path: Path, sch_path: Path):
        shutil.copy2(backup_path, sch_path)
```
Expose "restore last snapshot" as a one-click action in the sidecar UI's message history (each Tracer-authored change references the snapshot it created, so "undo this change" is a direct lookup, not a generic file-manager task for the user).

### 3.4 Bounding Box / Clear-Canvas Placement

This is the trickiest correctness problem in the whole S-expression layer, and worth its own subsection.

**Approach:**
1. Walk all existing `symbol`, `label`, `text`, and `wire` nodes and compute each element's axis-aligned bounding box in schematic coordinate space (KiCad schematic units are mm, origin top-left of the sheet by convention). For symbols this requires knowing the symbol's *graphical* extents, not just its anchor `at` position — pull this from the symbol's library definition (KiCad embeds or references `lib_symbols` in the `.kicad_sch` itself as of KiCad 6+, so you can extract pin/body extents without a separate library lookup in the common case).
2. Build a 2D occupancy grid (or, more precisely, a list of occupied rectangles) over the sheet's usable area (respecting the configured sheet size, e.g., A4/A3/Letter from the `paper` node).
3. For a new block of N components you're about to inject, compute its own aggregate bounding box (from `generator.py`'s already-decided relative layout), then search for the smallest-cost empty rectangle that fits it with a configurable clearance margin (KiCad's default grid is 1.27mm/50mil; pad clearance to at least 2 grid units around any existing element) — a simple **shelf/skyline bin-packing search** run left-to-right, top-to-bottom from the current rightmost/lowest-used extent is sufficient; you don't need true 2D bin-packing optimality, just "doesn't overlap anything and isn't absurdly far from the existing circuit."
4. If no clear region exists within the current sheet bounds, extend placement below the lowest existing element (schematics grow downward/rightward more gracefully than KiCad handles arbitrary resizing) rather than shrinking margins — a violated placement margin is a worse failure mode (visually cluttered, hard to route) than a slightly oversized sheet.

```python
# placement.py
from dataclasses import dataclass

@dataclass
class BBox:
    x_min: float; y_min: float; x_max: float; y_max: float

    def overlaps(self, other: "BBox", margin: float = 2.54) -> bool:
        return not (self.x_max + margin < other.x_min or
                    other.x_max + margin < self.x_min or
                    self.y_max + margin < other.y_min or
                    other.y_max + margin < self.y_min)

def find_clear_region(occupied: list[BBox], new_block_size: tuple[float, float],
                       sheet_size: tuple[float, float], margin: float = 2.54) -> tuple[float, float]:
    w, h = new_block_size
    step = 2.54  # one grid unit
    y = margin
    while y + h < sheet_size[1]:
        x = margin
        while x + w < sheet_size[0]:
            candidate = BBox(x, y, x + w, y + h)
            if not any(candidate.overlaps(occ, margin) for occ in occupied):
                return (x, y)
            x += step
        y += step
    # fall through: extend canvas downward beyond current lowest element
    lowest_y = max((occ.y_max for occ in occupied), default=0)
    return (margin, lowest_y + margin * 4)
```

### 3.5 Round-Trip Formatting Fidelity

A subtle but critical requirement: **KiCad's own S-expression writer has specific formatting conventions** (indentation, float precision, key ordering within nodes) that don't affect parsing correctness but *do* show up as noisy diffs in version control and can visually appear as "did Tracer touch things it shouldn't have" to a suspicious user reviewing a git diff. Your Layer 1 parser/serializer must:
- Preserve byte-identical output for any untouched node (i.e., don't round-trip through a lossy intermediate representation and reformat the entire file — mutate the tree in place and only re-serialize the touched region's containing top-level nodes if at all feasible, or accept full-file re-serialization but make the serializer's formatting rules match KiCad's own writer precisely).
- This is worth investing real engineering time in during Phase 2, because it directly determines whether users trust the tool's diffs.

---

## 4. End-to-End User Experience (UX Flow)

### 4.1 Step-by-Step Journey

**Step 1 — Launch & Anchor**
User launches Tracer Sidecar (via a pinned taskbar/dock shortcut, or `tracer.exe`/`.app` double-click). On first run, it prompts once for a preferred docking edge and remembers it (`QSettings`). It appears as a collapsed pill on the chosen screen edge, expanding on click. It performs initial project auto-discovery (§2.1) and shows a small status chip: `Editing: motor-driver.kicad_sch` or, if undetermined, `No project detected — drag a .kicad_pro here`.

**Step 2 — Natural-Language Prompt**
User types (or the panel is invoked via a global hotkey, e.g., `Ctrl+Shift+T`, to focus the input without alt-tabbing): *"Add an I2C OLED display with pull-ups and power filtering."* This is sent to the FastAPI `/chat` (or equivalent) endpoint, which routes to `agent.py`.

**Step 3 — Plan Review (critical trust checkpoint)**
Rather than injecting immediately, the agent's response streams back a **structured proposal**, rendered as a distinct card type in the chat panel (not just prose):
- **Component manifest:** table of new parts (`SSD1306 OLED`, `R1 4.7kΩ`, `R2 4.7kΩ`, `C1 100nF`) with footprint/value pre-filled from `generator.py`'s library resolution.
- **Netlist diff:** a "+" list of new nets/connections (`SDA → R1.1, U1.SDA`; `VCC → C1.1, U1.VCC`) rendered as a simple diff-style list (green `+` lines), not a paragraph.
- **Pin allocation:** which existing symbol pins the new block attaches to, flagged if any target pin is already occupied (hard stop, not a silent overwrite).
- **Placement preview:** a small inline SVG/canvas thumbnail (rendered client-side in the `QWebEngineView` from the bounding-box data in §3.4) showing where on the sheet the new block will land relative to existing symbols, *before* any file is touched.

User can: **Accept**, **Edit** (natural-language follow-up: *"use 10kΩ pull-ups instead"* — a multi-turn refinement against the *same* proposed plan, not a new one from scratch), or **Reject**.

**Step 4 — Execution & Reload Notification**
On Accept: Tracer snapshots the current file (§3.3), applies the mutation via Layer 3 (§3.1), re-serializes preserving formatting fidelity (§3.5), and writes to disk. Because KiCad does not auto-reload, the sidecar surfaces a clear, dismissible in-panel notification — not a modal dialog that steals focus from KiCad — reading something like: *"✅ Applied. Press Ctrl+Shift+F to refresh in KiCad."* (KiCad's actual manual-reload shortcut/menu action, confirmed against the installed version). Optionally, on supported platforms, Tracer can attempt to **programmatically trigger** KiCad's reload via OS-level UI automation (e.g., sending the reload keystroke to KiCad's window via `pyautogui`/platform accessibility APIs) as an opt-in "auto-refresh" setting — clearly flagged as best-effort/experimental since it depends on window focus and KiCad's window still existing at a stable, discoverable handle.

### 4.2 UX Principles Worth Calling Out Explicitly

- **Never auto-apply on the first response.** The plan-review step (Step 3) is non-negotiable even for "simple" requests — because Tracer is now writing files *from outside* the tool the user trusts, every write needs an explicit, inspectable confirmation gate. This is the single biggest trust-builder in the whole redesign.
- **Every applied change is one-click reversible** directly from its chat message (tied to its snapshot, §3.3) — undo should never require the user to go hunting in a `.tracer-bak` folder manually.
- **Status chip is always visible**, even collapsed to the pill state (e.g., a colored dot: green = target locked & confirmed, yellow = auto-detected but unconfirmed, red = no target) — the user should never wonder "which file is this thing about to edit."

---

## 5. Phased Implementation Roadmap

### Phase 1 — MVP Shell
**Goal:** Prove the floating-companion model end-to-end with the *existing* backend, minimal new complexity.

- PySide6 frameless/always-on-top window shell (§1.3) with a `QWebEngineView` chat surface pointed at a locally embedded FastAPI instance (§1.2 Option A).
- Manual project targeting only (drag-and-drop / file picker, §2.2) — **skip auto-discovery entirely in Phase 1**; it's the riskiest, most platform-fragile piece and isn't needed to validate the core UX loop.
- Direct file injection via the *existing* `tracer_inject.py` text-manipulation approach — do **not** attempt the S-expression AST rewrite in Phase 1; keep this phase's scope to "new shell, old injection engine" so you can validate the UX before touching the reliability-critical file-writing internals.
- No plan-review diff cards yet — a plain chat response plus a "Confirm write?" yes/no is enough to establish the confirm-before-write pattern.
- Manual-only reload notification (a static instructional message, no automation attempt).
- **Exit criteria:** a user can open the sidecar, target a schematic manually, ask for a component, confirm, and see it appear in KiCad after a manual refresh — with zero regressions vs. the current in-KiCad plugin's actual circuit-generation quality.

### Phase 2 — AST & Workspace Sync
**Goal:** Replace the fragile parts identified in Phase 1 with the robust, structural approach.

- Build the full `tracer_ast/` module (§3.1–3.5): parser, typed model, mutators, UUID registry, snapshot manager, placement engine. Port `generator.py` to call `mutators.py` instead of `tracer_inject.py`'s text templates.
- Round-trip formatting fidelity work (§3.5) — validate with a test suite of real-world `.kicad_sch` files run through parse→no-op-mutate→serialize and diffed against the original (target: zero unexpected diff lines).
- Implement auto-discovery (§2.1): window-title polling first (highest platform coverage on Win/macOS), recent-projects-file fallback, `watchdog`-based external-change detection layered on top of whichever target is established.
- Introduce the plan-review card UI (§4.1 Step 3) — component manifest + netlist diff, without the placement-preview thumbnail yet (that depends on the placement engine landing first, which it does in this same phase, so sequence: placement engine → thumbnail rendering).
- **Exit criteria:** injections are byte-fidelity-preserving, UUID-collision-proof, auto-rollback-capable, and the sidecar correctly identifies the active project on Windows/macOS without manual selection in the common single-project-open case.

### Phase 3 — Interactive Copilot
**Goal:** Close the remaining gap to a genuinely "sitting beside you, iterating with you" tool rather than a one-shot generator.

- Visual diff preview (§4.1 Step 3 placement thumbnail) rendered before every apply, using the bounding-box/clearance engine from Phase 2 to show exact injected-block placement on a lightweight canvas render of the current sheet.
- Multi-turn iterative editing: the agent must track the *pending proposed plan* as conversational state so "use 10kΩ instead" mutates the not-yet-applied plan rather than starting over — this likely requires `agent.py` to carry an explicit `PendingPlan` object across turns rather than treating each message as a fresh generation.
- Canvas clearance mapping surfaced proactively (not just used internally) — e.g., warning the user in the plan card if a requested placement is tight against an existing net, even below the hard-overlap threshold.
- Opt-in best-effort auto-reload automation (§4.1 Step 4) via OS-level window-focus + keystroke injection, gated behind an explicit settings toggle with a clear warning about its best-effort nature.
- Revisit the Tauri-with-Python-sidecar packaging question (§1.1) if PySide6/`QWebEngineView` bundle size or startup time has become a real user complaint by this point — this is a packaging optimization, not a functional requirement, so it belongs at the end of the roadmap, not the start.

---

## 6. Two Generation Pipelines: New-Project Wizard vs. Modify-Existing Companion

The original plan (§2–4) assumed a project already exists and Tracer is injecting into it. The actual intended product experience is broader — a user with *nothing open yet* types "build an ESP32 motor driver controller" and expects a finished project to appear. That's a distinct pipeline that needs to be designed alongside the injection pipeline, not bolted on after.

**Pipeline A — New Project Generation**
1. Agent designs the full circuit plan from the prompt (component set, netlist, rough sheet layout) — `generator.py`'s validation matters *more* here than in injection, since there's no existing human-drawn schematic to sanity-check against.
2. A new `project_scaffold.py` module writes a **minimal valid `.kicad_pro`** (JSON project settings — library table references, ERC/DRC rule defaults, sheet metadata) alongside the `.kicad_sch`, following KiCad's convention that the root sheet name matches the project name. A bare `.kicad_sch` with no accompanying `.kicad_pro` will open, but with degraded library resolution and no project-level settings — not what a user expects from "your first project."
3. User is prompted for a save location (native `QFileDialog`, directory picker) before any file is written.
4. Tracer launches KiCad with the new project path via `subprocess` — but first checks whether a KiCad instance is already running (per-OS process check) and opens into that instance rather than spawning a duplicate window.
5. MVP scope should be single-sheet only; multi-sheet hierarchical generation is a later-phase capability, not a Phase 1 requirement.

**Pipeline B — Modify-Existing Companion**
This is everything already specified in §2 (project detection) through §4 (injection UX flow) — the sidecar observes an already-open project and injects changes into it.

**Shared substrate:** both pipelines funnel through the same `tracer_ast` mutators, placement engine, and snapshot manager (§3) — only the entry point differs (scaffold-then-populate vs. detect-then-inject). Build that shared layer once; don't fork it per pipeline.

**Phasing correction:** Pipeline B (project detection, file watching) was the original Phase 1–2 focus, but it's actually the *harder* half. Pipeline A is simpler to bootstrap (no detection ambiguity — the user tells you the save path directly) and matches the actual first-touch product experience described in your flow: prompt → generated project → app opens → sidecar overlay appears for review/iteration. **Recommend folding a minimal Pipeline A into Phase 1** alongside the MVP shell, with the sidecar overlay appearing immediately after that first generation — rather than treating "generate a new project" and "the floating companion" as sequential, unrelated milestones.

---

## 7. Distribution, Licensing & Website Architecture

### 7.1 Site Structure
Standard SaaS marketing site: Home, Features, Pricing, Docs, Download, Login/Register. Two primary CTAs on the homepage (Download, Register) matches the pattern every tool in this space uses (Cursor, Cluely, Raycast) — nothing to reinvent here.

### 7.2 Backend for Auth & Billing
Given the team is two people and the backend is already FastAPI, **extend the existing FastAPI service with `/auth` and `/billing` routes rather than standing up a separate Node/Next.js backend.** Running two backend stacks for a two-person team is avoidable operational overhead; a static/lightweight marketing frontend can still be plain HTML/Next.js, it just calls the same FastAPI backend Tracer's desktop app already talks to.

### 7.3 Authentication
Don't roll your own auth — use a managed provider (Clerk, Auth0, or Supabase Auth). The detail that matters specifically for a **desktop app**: it can't safely embed a client secret or a login webview the way a website can. The standard pattern (used by GitHub CLI, Docker Desktop, and similar tools) is:
1. Desktop app opens the user's system browser to your hosted login page.
2. App spins up a temporary localhost server on an ephemeral port (or uses the OAuth device-authorization grant) to receive the redirect after login.
3. On success, the app receives a signed session token/JWT, stored in the OS keychain (not plaintext on disk).
4. That token is sent on every request to the FastAPI backend, which checks the user's entitlement/tier.

### 7.4 Billing — Stripe
("Stripe or Stride" — almost certainly Stripe; it's the standard choice here.) Use **Stripe Checkout** (hosted payment page — keeps you out of PCI-compliance scope entirely) for signup, and the **Stripe Customer Portal** for self-serve plan changes/cancellation. Handle the `checkout.session.completed` and `customer.subscription.updated` / `.deleted` webhooks to update the user's tier in your own database — that row is what both the website and the desktop app check to gate features.

**Define tiers by concrete limits, not just price**, before building the paywall logic — e.g.:
- **Free:** capped generations/month, no vision-based screenshot review.
- **$10 tier:** higher generation cap, vision review enabled.
- **$20 tier:** highest cap, priority/faster model routing.

This matters mechanically because `agent.py` needs an entitlement check *before* deciding whether to route a request to the (often more expensive) vision-capable model call.

### 7.5 Staging Recommendation
Since real users right now are just you and one acquaintance: **don't build the full Stripe/tier machinery yet.** Hardcode both of your accounts to the top entitlement tier while you build Pipelines A/B and the sidecar itself, and wire up real Stripe checkout only when you're ready to open registration to the public. Building the billing layer before the core product works is the classic overengineering trap worth avoiding here.

---

## Key Trade-offs Summary

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| UI shell | PySide6 + embedded `QWebEngineView` | wxPython | Better dark-theme control, better rich-chat rendering |
| UI shell | PySide6 + embedded `QWebEngineView` | Full Electron | Avoids duplicate runtime; backend is already Python |
| Backend process model | Embedded FastAPI thread (Phase 1–2) | Standalone daemon from day one | Simpler MVP; daemon deferred to Phase 3 when multi-turn state persistence matters |
| File mutation | Structural S-expression AST (Phase 2+) | Continued text-block replacement | Text replacement is version-fragile and can't guarantee UUID/placement correctness |
| Project detection | Layered signal fusion (title + recents + watchdog) with manual override always available | Any single "authoritative" auto-detection mechanism | No single signal is reliable across Win/macOS/Linux/Wayland; manual override is the trust backstop |
| Reload | Manual notification by default, automation opt-in later | Attempting automation from Phase 1 | Automation is fragile/best-effort and shouldn't gate the MVP's exit criteria |
| Real-time schematic sync | File write + reload notification/automation | True live IPC sync | KiCad's IPC API doesn't cover the schematic editor yet (PCB editor only, as of KiCad 10) — not achievable today regardless of engineering effort |
| Screenshot review overlay | Plain window capture + vision LLM, no stealth | Interview-Coder-style capture-exclusion overlay | No third party to hide from — the user is reviewing their own work, not deceiving anyone watching |
| Auth for desktop app | System-browser OAuth loopback via managed provider (Clerk/Auth0) | Embedded login webview / custom auth | Desktop apps can't safely hold client secrets; the loopback pattern is the established solution |
| Billing rollout | Hardcode top tier for the two current users now; build Stripe when opening to the public | Building full tier/paywall logic immediately | Avoids engineering the billing layer before the core product (Pipelines A/B) is proven |
