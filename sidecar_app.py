"""Tracer Sidecar -- Phase 1 overlay.

Two frameless, always-on-top panels styled after ALT TAB Circuit Copilot:
  1. Chat panel (right edge)
  2. Input dock (bottom, wider)

Both snap to KiCad's window edge on Windows via pywin32 (falls back to
primary-screen edges if KiCad isn't open). After a successful injection,
we send Ctrl+F5 to KiCad so the schematic view refreshes without the user
alt-tabbing.
"""

from __future__ import annotations

import os

# pythonw has no GPU context — WebEngine renders blank/invisible without this.
os.environ.setdefault("QT_OPENGL", "software")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --disable-gpu-compositing --no-sandbox",
)

import signal
import argparse
import socket
import sys
import threading
import time
import traceback
import ctypes
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn

from PySide6.QtCore import (
    QEvent, QObject, QPoint, QRect, QSize, Qt, QThread, QTimer, QUrl, Signal, Slot,
)
from PySide6.QtGui import QColor, QGuiApplication, QRegion
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QMainWindow,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebChannel import QWebChannel

from app import app as fastapi_app
import agent
import tracer_auth
import tracer_api

sys.path.insert(0, str(Path(__file__).parent / "tracer_plugin"))
import tracer_inject  # noqa: E402


BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8000
HERE = Path(__file__).parent
CHAT_HTML = HERE / "assets" / "chat.html"
DOCK_HTML = HERE / "assets" / "dock.html"
_INSTANCE_MUTEX = None


def _acquire_instance() -> bool:
    global _INSTANCE_MUTEX
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.windll.kernel32
    _INSTANCE_MUTEX = kernel32.CreateMutexW(
        None, True, "Local\\TracerEngineSidecar")
    if not _INSTANCE_MUTEX:
        return False
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(_INSTANCE_MUTEX)
        _INSTANCE_MUTEX = None
        return False
    return True


# ============================================================================
# background FastAPI (unchanged)
# ============================================================================


class BackendServer:
    def __init__(self, host: str = BACKEND_HOST, port: int = BACKEND_PORT) -> None:
        if port and not self._port_available(host, port):
            port = 0
        config = uvicorn.Config(
            fastapi_app, host=host, port=port,
            log_level="warning", access_log=False, lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None
        self._thread = threading.Thread(
            target=self._server.run, name="tracer-backend", daemon=True)

    @staticmethod
    def _port_available(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError:
                return False
        return True

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=timeout)


# ============================================================================
# workers
# ============================================================================


class GenerateWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)
    log_line = Signal(str)

    def __init__(self, prompt: str, history: List[Dict[str, str]],
                 attachments: Optional[List[Dict[str, str]]] = None) -> None:
        super().__init__()
        self._prompt = prompt
        self._history = history
        self._attachments = attachments or []
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        try:
            def log(line: str) -> None:
                if not self._cancelled:
                    self.log_line.emit(line)
            # Slice I: cloud path. tracer_api mirrors agent.py's return
            # shape so downstream _on_generate_finished sees the same
            # {status, circuit, backend, history} dict.
            result = tracer_api.build_with_auto_repair(
                self._prompt, history=self._history, log=log,
                attachments=self._attachments)
            if self._cancelled:
                self.failed.emit("cancelled")
                return
            self.finished.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


class AskWorker(QObject):
    """Ask (Q&A) or Plan (design outline): plain-text LLM turn, no circuit
    gen, no backend, no injection. The mode-specific system prompt lives in
    agent.py's ask_gemini / plan_gemini wrappers."""
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, prompt: str, history: List[Dict[str, str]],
                 kind: str = "ask",
                 attachments: Optional[List[Dict[str, str]]] = None) -> None:
        super().__init__()
        self._prompt = prompt
        self._history = history
        self._kind = kind
        self._attachments = attachments or []

    @Slot()
    def run(self) -> None:
        try:
            fn = tracer_api.plan_gemini if self._kind == "plan" else tracer_api.ask_gemini
            reply = fn(self._prompt, self._history,
                       attachments=self._attachments)
            self.finished.emit(reply)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class InjectWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, generated_path: str, target_path: str) -> None:
        super().__init__()
        self._generated = generated_path
        self._target = target_path

    @Slot()
    def run(self) -> None:
        try:
            summary = tracer_inject.inject(
                generated_path=self._generated, target_path=self._target)
            summary["text"] = tracer_inject.describe(summary)
            self.finished.emit(summary)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def _find_kicad_cli() -> Optional[str]:
    import shutil
    hit = shutil.which("kicad-cli")
    if hit:
        return hit
    # Windows install default.
    for ver in ("10.0", "9.0", "8.0"):
        p = Path(f"C:/Program Files/KiCad/{ver}/bin/kicad-cli.exe")
        if p.is_file():
            return str(p)
    return None


class PcbDrcWorker(QObject):
    """kicad-cli pcb drc --schematic-parity --format json <pcb>.

    Emits `done` with {ok, errors, warnings, violations:[{severity,type,items:[str]}]}.
    kicad-cli returns nonzero when violations exist; we still parse the JSON.
    """
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, pcb_path: str, cli_path: str) -> None:
        super().__init__()
        self._pcb = pcb_path
        self._cli = cli_path

    @Slot()
    def run(self) -> None:
        import subprocess, json as _json, tempfile
        try:
            with tempfile.NamedTemporaryFile(
                    "w+", suffix=".json", delete=False, encoding="utf-8") as tf:
                out_path = tf.name
            cmd = [self._cli, "pcb", "drc",
                   "--schematic-parity", "--all-track-errors",
                   "--severity-all", "--format", "json",
                   "--output", out_path, self._pcb]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60)
            try:
                data = _json.loads(Path(out_path).read_text(encoding="utf-8"))
            except (OSError, _json.JSONDecodeError):
                self.failed.emit(
                    f"kicad-cli exit={proc.returncode}; "
                    f"stderr={(proc.stderr or '').strip()[:400]}")
                return
            # Shape (KiCad 10 DRC JSON): {violations:[{severity,type,description,items:[{...}]}], unconnected_items:[...], schematic_parity:[...]}
            v = list(data.get("violations") or [])
            v += list(data.get("unconnected_items") or [])
            parity = list(data.get("schematic_parity") or [])
            v += parity
            errors = sum(1 for x in v if (x.get("severity") or "").lower() == "error")
            warnings = sum(1 for x in v if (x.get("severity") or "").lower() == "warning")
            self.done.emit({
                "ok": errors == 0,
                "errors": errors,
                "warnings": warnings,
                "parity_mismatches": len(parity),
                "violations": v[:32],  # cap for UI
            })
        except FileNotFoundError:
            self.failed.emit("kicad-cli not found")
        except subprocess.TimeoutExpired:
            self.failed.emit("kicad-cli DRC timed out (>60s)")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# ============================================================================
# KiCad window detection + reload keystroke (Windows via pywin32)
# ============================================================================

KICAD_PROCS = ("kicad.exe", "eeschema.exe", "pcbnew.exe", "kicad-cli.exe")
SIDECAR_PROCS = ("python.exe", "pythonw.exe")
# Windows parks minimized windows at (-32000, -32000); IsIconic can lag.
_MINIMIZED_COORD = -30000


def _project_from_title(title: str) -> str:
    """'tracertest — PCB Editor' -> 'tracertest'. Empty title -> ''."""
    if not title:
        return ""
    for sep in (" — ", " – ", " - "):  # em-dash, en-dash, hyphen
        if sep in title:
            return title.split(sep, 1)[0].strip()
    return title.strip()


assert _project_from_title("tracertest — PCB Editor") == "tracertest"
assert _project_from_title("tracertest - Schematic Editor") == "tracertest"
assert _project_from_title("") == ""
assert _project_from_title("KiCad Project Manager") == "KiCad Project Manager"


def _proc_name_for_hwnd(hwnd: int) -> str:
    try:
        import win32process
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        h = ctypes.windll.kernel32.OpenProcess(0x1000, 0, pid)
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.c_ulong(260)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(
            h, 0, buf, ctypes.byref(size))
        ctypes.windll.kernel32.CloseHandle(h)
        return Path(buf.value).name.lower()
    except Exception:
        return ""


def _rect_is_minimized(x1: int, y1: int) -> bool:
    return x1 <= _MINIMIZED_COORD or y1 <= _MINIMIZED_COORD


def _window_is_minimized(hwnd: int) -> bool:
    try:
        import win32gui
    except ImportError:
        return False
    if win32gui.IsIconic(hwnd):
        return True
    x1, y1, _, _ = win32gui.GetWindowRect(hwnd)
    return _rect_is_minimized(x1, y1)


class KiCadWatcher(QObject):
    """Finds KiCad's main window (schematic or PCB editor) and reports its
    screen geometry so overlay panels can snap to it."""

    changed = Signal(dict)   # {"rect": QRect, "title": str} or {"rect": None}

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._poll)
        self._last_key: Optional[tuple] = None

    def start(self) -> None:
        self._timer.start()
        self._poll()

    def stop(self) -> None:
        self._timer.stop()

    def _poll(self) -> None:
        try:
            info = self._find_kicad_window()
        except Exception as exc:  # noqa: BLE001
            print(f"[watcher] poll failed: {exc}", file=sys.stderr)
            return
        key = (info.get("title"), info.get("rect")) if info else None
        if key != self._last_key:
            self._last_key = key
            self.changed.emit(info or {"rect": None})

    def find_open_schematic(self) -> Optional[Path]:
        """Auto-detect the .kicad_sch that's currently open in Eeschema.

        KiCad's Schematic Editor titles look like
        `foo.kicad_sch — Schematic Editor`. We parse the filename out of the
        title, then resolve the full path via KiCad's recent-projects config
        (%APPDATA%/kicad/<ver>/kicad_common.json) which lists the last-opened
        projects with absolute paths.
        """
        try:
            import win32gui
            import ctypes
        except ImportError:
            return None
        import os
        import re
        import json

        fname = None
        pcb_fallback: Optional[str] = None
        def cb(hwnd, _):
            nonlocal fname, pcb_fallback
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd) or ""
            low = title.lower()
            is_sch = "schematic editor" in low or "eeschema" in low
            is_pcb = "pcb editor" in low or "pcbnew" in low
            if not (is_sch or is_pcb):
                return
            m = re.search(r"([^\\/]+?\.kicad_sch)", title)
            if m:
                fname = m.group(1)
                return
            # KiCad 10 titles like "tracertest — Schematic Editor" (or PCB
            # Editor, with an optional leading "*" for dirty buffer) drop the
            # extension. Recover the project name and synthesize the .kicad_sch
            # name; the recent-projects resolver below confirms it on disk.
            # Schematic Editor wins if both editors are open — sch is the
            # canonical inject target; PCB is a fallback.
            proj = _project_from_title(title).lstrip("*").strip()
            if not proj:
                return
            synth = f"{proj}.kicad_sch"
            if is_sch:
                fname = synth
            elif pcb_fallback is None:
                pcb_fallback = synth
        win32gui.EnumWindows(cb, None)
        fname = fname or pcb_fallback
        if not fname:
            return None

        # Resolve full path via KiCad's config.
        appdata = os.environ.get("APPDATA", "")
        candidates: List[Path] = []
        if appdata:
            for ver in ("10.0", "9.0", "8.0"):
                p = Path(appdata) / "kicad" / ver / "kicad_common.json"
                if p.is_file():
                    candidates.append(p)
        # Also try walking common KiCad project dirs directly.
        for cfg in candidates:
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            recent = (data.get("system") or {}).get("recent_projects") or \
                     data.get("recent_projects") or []
            for entry in recent:
                if isinstance(entry, str):
                    proj_path = entry
                elif isinstance(entry, dict):
                    proj_path = entry.get("path") or entry.get("file") or ""
                else:
                    continue
                if not proj_path:
                    continue
                # kicad_common lists .kicad_pro paths -- schematic sits alongside.
                pp = Path(proj_path)
                sch = pp.with_suffix(".kicad_sch")
                if sch.name == fname and sch.is_file():
                    return sch
                sch_in_dir = pp.parent / fname
                if sch_in_dir.is_file():
                    return sch_in_dir

        # KiCad's per-editor file histories (eeschema.json / pcbnew.json /
        # kicad.json) hold absolute paths of recently-opened files, most
        # recent first — the authoritative source when kicad_common.json's
        # recent_projects is empty. eeschema history gives us a .kicad_sch
        # directly; pcbnew/kicad histories give sibling paths.
        if appdata:
            for ver in ("10.0", "9.0", "8.0"):
                for name, suffix in (
                    ("eeschema.json", ".kicad_sch"),
                    ("pcbnew.json",   ".kicad_pcb"),
                    ("kicad.json",    ".kicad_pro"),
                ):
                    hist_cfg = Path(appdata) / "kicad" / ver / name
                    if not hist_cfg.is_file():
                        continue
                    try:
                        hdata = json.loads(hist_cfg.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    hist = (hdata.get("system") or {}).get("file_history") or []
                    for entry in hist:
                        if not isinstance(entry, str) or not entry:
                            continue
                        pp = Path(entry)
                        sch = pp.with_suffix(".kicad_sch")
                        if sch.name == fname and sch.is_file():
                            return sch

        # Last-ditch: scan common Documents/KiCad tree for a matching file.
        home_kicad = Path.home() / "Documents" / "KiCad"
        if home_kicad.is_dir():
            for p in home_kicad.rglob(fname):
                return p  # first match wins
        return None

    @staticmethod
    def _collect_kicad_hits(*, include_minimized: bool) -> List[Dict[str, Any]]:
        try:
            import win32gui
        except ImportError:
            return []
        hits: List[Dict[str, Any]] = []
        MATCH_PHRASES = (
            "— schematic editor", "- schematic editor",
            "— pcb editor",       "- pcb editor",
            "— eeschema",         "- eeschema",
            "— pcbnew",           "- pcbnew",
            "kicad project manager",
        )
        EXCLUDE_PROC = ("chrome.exe", "msedge.exe", "firefox.exe",
                        "brave.exe", "opera.exe")

        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            t = title.lower()
            proc = _proc_name_for_hwnd(hwnd)
            if proc in EXCLUDE_PROC or proc in SIDECAR_PROCS:
                return
            if title.strip().lower() in ("tracer", "tracer copilot"):
                return
            phrase_hit = any(p in t for p in MATCH_PHRASES)
            proc_hit = proc in KICAD_PROCS
            if not (phrase_hit or proc_hit):
                return
            if not include_minimized and _window_is_minimized(hwnd):
                return
            x1, y1, x2, y2 = win32gui.GetWindowRect(hwnd)
            w, h = x2 - x1, y2 - y1
            hits.append({"hwnd": hwnd, "title": title,
                         "rect": (x1, y1, x2, y2),
                         "proc": proc, "usable": w > 400 and h > 300})
        win32gui.EnumWindows(cb, None)
        return hits

    def _find_kicad_window(self) -> Optional[Dict[str, Any]]:
        hits = self._collect_kicad_hits(include_minimized=False)
        # First call: dump what we found for debugging.
        if not getattr(self, "_dumped", False):
            self._dumped = True
            print(f"[watcher] KiCad scan: {len(hits)} candidate(s)",
                  file=sys.stderr)
            for h in hits:
                print(f"  proc={h['proc']!r}  title={h['title']!r}",
                      file=sys.stderr)
        if not hits:
            return None
        # Prefer schematic > pcb > any KiCad
        def rank(h):
            t = h["title"].lower()
            if "schematic" in t or "eeschema" in t:
                return 0
            if "pcb" in t or "pcbnew" in t:
                return 1
            return 2
        if not hits:
            return None
        hits.sort(key=rank)
        pick = hits[0]
        if not pick["usable"]:
            return None
        r = pick["rect"]
        # r is (x1, y1, x2, y2) from GetWindowRect, convert to QRect(x, y, width, height)
        width = r[2] - r[0]
        height = r[3] - r[1]
        # Clamp to screen bounds to prevent off-screen positioning
        screen = QGuiApplication.primaryScreen()
        if screen:
            screen_rect = screen.availableGeometry()
            x = max(screen_rect.left(), min(r[0], screen_rect.right() - width))
            y = max(screen_rect.top(), min(r[1], screen_rect.bottom() - height))
            width = min(width, screen_rect.width())
            height = min(height, screen_rect.height())
        else:
            x, y = r[0], r[1]
        return {"rect": QRect(x, y, width, height),
            "title": pick["title"], "hwnd": pick["hwnd"],
            "hwnds": {hit["hwnd"] for hit in hits}}


def send_reload_to_kicad() -> bool:
    """Ask the frontmost KiCad window to reload the current schematic.

    KiCad's Eeschema File > Revert has no default shortcut, but sending
    Ctrl+F5 is a common browser-style refresh binding many users add. If
    that doesn't work, at least focusing KiCad tells the user the file
    changed. Best-effort; returns False if pywin32 is missing.
    """
    try:
        import win32gui, win32con, win32api  # noqa
    except ImportError:
        return False
    # Find KiCad's Schematic Editor and bring it forward.
    watcher = KiCadWatcher()
    info = watcher._find_kicad_window()
    if not info:
        return False
    try:
        hwnd = None
        def cb(h, _):
            nonlocal hwnd
            if not win32gui.IsWindowVisible(h):
                return
            t = win32gui.GetWindowText(h).lower()
            if "schematic editor" in t or "eeschema" in t:
                hwnd = h
        win32gui.EnumWindows(cb, None)
        if hwnd is None:
            return False
        win32gui.SetForegroundWindow(hwnd)
        # Send Ctrl+F5 (works for many KiCad-user-bound revert shortcuts).
        # Users without the binding can still see KiCad focus and Revert manually.
        VK_CONTROL = 0x11
        VK_F5 = 0x74
        win32api.keybd_event(VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(VK_F5, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(VK_F5, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def foreground_is_allowed(foreground_hwnd: int, kicad_hwnds: set[int],
              tracer_hwnds: set[int],
              kicad_foreground: bool = False) -> bool:
    """Return whether Tracer may remain visible for the foreground window."""
    kicad_is_open = bool(kicad_hwnds)
    return (kicad_foreground or
        foreground_hwnd in kicad_hwnds or
        (kicad_is_open and foreground_hwnd in tracer_hwnds))


# ============================================================================
# JS <-> Python bridge
# ============================================================================


class PyBridge(QObject):
    """Shared bridge exposed to BOTH the chat panel and the dock."""

    # -> JS
    userMessage = Signal(str)
    assistantMessage = Signal("QVariant")
    activityAdded = Signal("QVariant")
    changeAdded = Signal("QVariant")
    thinkingChanged = Signal(bool)
    targetChanged = Signal(str)
    kicadStatusChanged = Signal(str)
    chatCleared = Signal()
    changesFinalized = Signal(bool)
    modelsAvailable = Signal("QVariant", str)
    dockInit = Signal("QVariant")
    showUndoModal = Signal("QVariant")   # -> chat panel
    clarificationChanged = Signal(bool)  # -> dock (freezes mode dropdown)
    attachmentsChanged = Signal("QVariant")   # -> dock (chip list)
    authStateChanged = Signal("QVariant")     # -> chat panel + dock; login flow state

    def __init__(self, shell: "OverlayShell") -> None:
        super().__init__()
        self._shell = shell

    # ---- init requests from views ----

    @Slot()
    def requestInit(self) -> None:
        # Chat panel finished loading.
        self._shell.push_kicad_status()

    @Slot()
    def requestDockInit(self) -> None:
        self._shell.push_dock_init()

    # ---- window controls ----

    @Slot()
    def closeWindow(self) -> None:
        self._shell.close_all()

    @Slot()
    def toggleMenu(self) -> None:
        # Reserved for a future settings menu.
        pass

    @Slot(str)
    def startDrag(self, which: str) -> None:
        w = self._shell.chat if which == "chat" else self._shell.dock
        if w is None: return
        handle = w.windowHandle()
        if handle is not None:
            handle.startSystemMove()

    # ---- input dock controls ----

    @Slot(str)
    def sendPrompt(self, prompt: str) -> None:
        self._shell.send_prompt(prompt)

    @Slot()
    def cancel(self) -> None:
        self._shell.cancel_current()

    @Slot()
    def pickAttachmentFile(self) -> None:
        self._shell.pick_attachment_file()

    @Slot()
    def captureKicadScreenshot(self) -> None:
        self._shell.capture_kicad_screenshot()

    @Slot(int)
    def removeAttachment(self, index: int) -> None:
        self._shell.remove_attachment(index)

    @Slot(str)
    def setMode(self, mode: str) -> None:
        self._shell.mode = mode

    @Slot(str)
    def setModel(self, name: str) -> None:
        self._shell.set_forced_model(name)

    @Slot(bool)
    def setAutoErc(self, on: bool) -> None:
        self._shell.auto_erc = on

    @Slot(bool)
    def setDockExpanded(self, expanded: bool) -> None:
        self._shell.set_dock_expanded(expanded)

    @Slot(str)
    def copyToClipboard(self, text: str) -> None:
        # navigator.clipboard fails in QWebEngineView (setHtml pages aren't
        # a "secure context") — route through Qt's own clipboard.
        print(f"[bridge] copyToClipboard len={len(text or '')}", file=sys.stderr, flush=True)
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(text or "")
            print("[bridge] copied to Qt clipboard", file=sys.stderr, flush=True)
        else:
            print("[bridge] QGuiApplication.clipboard() returned None", file=sys.stderr, flush=True)

    @Slot(result="QVariant")
    def listUndoHistory(self) -> List[Dict[str, Any]]:
        return self._shell.list_undo_history()

    @Slot(int, result="QVariant")
    def undoTo(self, index: int) -> Dict[str, Any]:
        return self._shell.undo_to(index)

    @Slot()
    def requestUndoModal(self) -> None:
        # Dock triggers this; chat panel listens on showUndoModal and renders.
        self.showUndoModal.emit(self._shell.list_undo_history())

    # ---- auth ----

    @Slot()
    def requestAuthState(self) -> None:
        self._shell.push_auth_state()

    @Slot()
    def login(self) -> None:
        self._shell.start_login()

    @Slot()
    def cancelLogin(self) -> None:
        self._shell.cancel_login()

    @Slot()
    def logout(self) -> None:
        self._shell.logout()

    @Slot()
    def openAccountPage(self) -> None:
        print("[bridge] openAccountPage slot entered", file=sys.stderr, flush=True)
        self._shell.open_account_page()

    @Slot()
    def pickTarget(self) -> None:
        self._shell.pick_target()

    @Slot()
    def newConversation(self) -> None:
        self._shell.new_conversation()

    @Slot()
    def keepChanges(self) -> None:
        self._shell.keep_changes()

    @Slot()
    def discardChanges(self) -> None:
        self._shell.discard_changes()


# ============================================================================
# Overlay windows
# ============================================================================


class OverlayWindow(QMainWindow):
    """Frameless tool window with an optional HTML header drag zone."""

    def __init__(self, html_path: Path, bridge: PyBridge,
                 drag_height: int = 0) -> None:
        super().__init__()
        # Strip Windows borders
        # Stay above KiCad while visible; focus watcher hides us when KiCad
        # loses foreground so we do not float over Chrome/VS Code.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowTitle("Tracer Copilot")
        # Force window background to be transparent
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAcceptDrops(True)
        self._drag_height = drag_height
        self._drag_offset: Optional[QPoint] = None

        self._view = QWebEngineView(self)
        self._view.installEventFilter(self)
        self.setCentralWidget(self._view)

        self._view.page().setBackgroundColor(QColor(0, 0, 0, 0))
        settings = self._view.page().settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)

        self._channel = QWebChannel(self._view.page())
        self._channel.registerObject("pyBridge", bridge)
        self._view.page().setWebChannel(self._channel)

        file_url = QUrl.fromLocalFile(str(html_path.resolve()))
        self._view.load(file_url)
        self._close_callback = None

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._view and self._drag_height:
            if event.type() == QEvent.MouseButtonPress:
                point = event.position().toPoint()
                if point.y() <= self._drag_height:
                    self.mousePressEvent(event)
            elif event.type() == QEvent.MouseMove and self._drag_offset:
                self.mouseMoveEvent(event)
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def set_close_callback(self, fn) -> None:
        self._close_callback = fn

    def closeEvent(self, event) -> None:
        if self._close_callback:
            self._close_callback()
        event.accept()


# ============================================================================
# Overlay Shell (owns state, coordinates windows)
# ============================================================================


class OverlayShell:
    def __init__(self, backend: BackendServer) -> None:
        self._backend = backend
        self._history: List[Dict[str, str]] = []
        self._schematic_path: Optional[Path] = None

        self._gen_thread: Optional[QThread] = None
        self._current_prompt: str = ""
        self._undo_ledger: List[Dict[str, Any]] = []
        # When Agent asks a clarification, the next user turn continues that
        # exchange even if the mode dropdown is on Plan/Ask. The UI freezes
        # the dropdown while this is set.
        self._pending_clarification: bool = False
        # Multimodal attachments queued for the next send: list of dicts
        # {"name": str, "path": str}. Images only for now (PNG/JPG/WEBP).
        self._pending_attachments: List[Dict[str, str]] = []
        self._MAX_ATTACHMENTS = 3

        # Auth: JWT loaded from disk if present. Login flow spawns a
        # DeviceFlow instance while active; cancel() stops the polling loop.
        self._auth_token: Optional[Dict[str, Any]] = tracer_auth.load_token()
        self._auth_flow: Optional[tracer_auth.DeviceFlow] = None
        self._auth_pending: Optional[Dict[str, Any]] = None  # user_code / uri while polling
        self._gen_worker: Optional[GenerateWorker] = None
        self._inj_thread: Optional[QThread] = None
        self._inj_worker: Optional[InjectWorker] = None

        self._activity_count = 0
        self._change_count = 0
        self._pending_plan: Optional[Dict[str, Any]] = None

        self.mode = "agent"
        self.auto_erc = True
        self._forced_model: Optional[str] = None

        self.bridge = PyBridge(self)

        # Two overlay windows
        self.chat = OverlayWindow(CHAT_HTML, self.bridge, drag_height=48)
        self.dock = OverlayWindow(DOCK_HTML, self.bridge)
        self.chat.resize(QSize(360, 620))
        self.dock.resize(QSize(720, 130))
        # Closing either window shuts the whole shell.
        self.chat.set_close_callback(self.close_all)
        self.dock.set_close_callback(self.close_all)

        # KiCad watcher: reposition overlays to snap to KiCad's window
        self._kicad_rect: Optional[QRect] = None
        self._kicad_title: str = ""
        self._kicad_hwnds: set[int] = set()
        self._kicad_gone_polls = 0
        self._kicad_minimized = False
        self._hidden_for_focus = False
        self._awaiting_first_present = True
        self._focus_grace_until = time.time() + 8.0
        self._watcher = KiCadWatcher()
        self._watcher.changed.connect(self._on_kicad_changed)
        self._focus_timer = QTimer(self._watcher)
        self._focus_timer.setInterval(250)
        self._focus_timer.timeout.connect(self._poll_focus)

    def show(self) -> None:
        self._watcher.start()
        self._focus_timer.start()
        self._reposition()
        self._present_panels(force=True)
        QTimer.singleShot(200, self._present_panels)
        QTimer.singleShot(500, self._reposition)
        QTimer.singleShot(500, self._present_panels)
        # Initial auto-target detection so the chip isn't stuck on "detecting…"
        detected = self._watcher.find_open_schematic()
        if detected:
            self._schematic_path = detected
            self.bridge.targetChanged.emit(detected.name)

    def close_all(self) -> None:
        self._watcher.stop()
        self._focus_timer.stop()
        for w in (self.chat, self.dock):
            w.close()
        QApplication.instance().quit()

    # ---------------- KiCad snap ----------------

    def _on_kicad_changed(self, info: Dict[str, Any]) -> None:
        # Wrapped: anything raised here would otherwise kill the QTimer
        # loop and, on some Qt builds, the whole app.
        try:
            rect = info.get("rect")
            self._kicad_rect = rect if isinstance(rect, QRect) else None
            self._kicad_title = info.get("title", "") or ""
            # Trust the live watcher scan only. Pinning --kicad-hwnd caused
            # hwnds=0 -> hwnds=1 oscillation (flash on close, snap-back on
            # minimize) because IsWindow() outlives the editor briefly.
            self._kicad_hwnds = set(info.get("hwnds", ()))
            all_hits = self._watcher._collect_kicad_hits(include_minimized=True)
            kicad_open = bool(all_hits)
            self._kicad_minimized = kicad_open and self._kicad_rect is None
            if kicad_open:
                self._kicad_gone_polls = 0
            else:
                self._kicad_gone_polls += 1
            print(
                f"[snap] KiCad rect={self._kicad_rect!r} "
                f"hwnds={len(self._kicad_hwnds)} open={kicad_open} "
                f"minimized={self._kicad_minimized} "
                f"gone={self._kicad_gone_polls}",
                file=sys.stderr,
            )
            self.push_kicad_status()
            if self._kicad_rect is not None and not self._kicad_minimized:
                self._reposition()
            self._sync_panel_visibility()
            if not kicad_open and self._kicad_gone_polls >= 4:
                # ~2s at 500ms watcher interval — KiCad closed, exit sidecar.
                print("[snap] KiCad gone — shutting down sidecar", file=sys.stderr)
                self.close_all()
                return
        except Exception as exc:  # noqa: BLE001
            print(f"[snap] reposition failed: {exc}", file=sys.stderr)
        # Auto-target detection: separate try so a scan bug doesn't clobber snap.
        try:
            detected = self._watcher.find_open_schematic()
            if detected and detected != self._schematic_path:
                self._schematic_path = detected
                self.bridge.targetChanged.emit(detected.name)
                self._add_activity("Sys", f"auto-target: {detected.name}")
            elif not detected and self._schematic_path is not None:
                self._schematic_path = None
                self.bridge.targetChanged.emit("")
            # Chip fallback: no .kicad_sch open, but a KiCad editor is —
            # show its project name so the chip isn't stuck on "detecting…".
            if self._schematic_path is None:
                self.bridge.targetChanged.emit(_project_from_title(self._kicad_title))
        except Exception as exc:  # noqa: BLE001
            print(f"[auto-target] scan failed: {exc}", file=sys.stderr)

    def _present_panels(self, force: bool = False) -> None:
        """Show panels above KiCad once geometry is known (launch path)."""
        if not force and (
            self._kicad_minimized or self._kicad_rect is None or not self._kicad_hwnds
        ):
            return
        if self._kicad_rect is not None:
            self._reposition()
        self._hidden_for_focus = False
        for w in (self.chat, self.dock):
            w.setWindowState(w.windowState() & ~Qt.WindowMinimized)
            w.showNormal()
            w.show()
            w.raise_()
        self._awaiting_first_present = False
        print(
            f"[focus] presented chat={self.chat.geometry()} "
            f"dock={self.dock.geometry()} "
            f"vis={self.chat.isVisible()}/{self.dock.isVisible()}",
            file=sys.stderr,
        )

    def _sync_panel_visibility(self) -> None:
        if self._awaiting_first_present:
            self._present_panels()
            return
        if self._kicad_minimized or self._kicad_rect is None or not self._kicad_hwnds:
            if not self._hidden_for_focus:
                self._hidden_for_focus = True
                self.chat.hide()
                self.dock.hide()
            return
        self._poll_focus()

    def _poll_focus(self) -> None:
        if sys.platform != "win32":
            return
        if time.time() < self._focus_grace_until:
            if self._kicad_rect is not None:
                self._present_panels(force=True)
            return
        if self._kicad_minimized or self._kicad_rect is None or not self._kicad_hwnds:
            if not self._hidden_for_focus:
                self._hidden_for_focus = True
                self.chat.hide()
                self.dock.hide()
            return
        try:
            import win32gui, win32process
            foreground = win32gui.GetForegroundWindow()
            kicad_pids = {
                win32process.GetWindowThreadProcessId(h)[1]
                for h in self._kicad_hwnds
            }
            foreground_pid = (win32process.GetWindowThreadProcessId(foreground)[1]
                              if foreground else 0)
            kicad_foreground = (
                foreground in self._kicad_hwnds or
                foreground_pid in kicad_pids)
            tracer_hwnds = {int(self.chat.winId()), int(self.dock.winId())}
            tracer_foreground = foreground in tracer_hwnds
        except ImportError:
            return

        should_show = kicad_foreground or tracer_foreground
        if should_show == (not self._hidden_for_focus):
            return
        self._hidden_for_focus = not should_show
        if should_show:
            self._reposition()
            self.chat.show()
            self.dock.show()
            self.chat.raise_()
            self.dock.raise_()
        else:
            self.chat.hide()
            self.dock.hide()

    def push_kicad_status(self) -> None:
        if self._kicad_rect:
            t = self._kicad_title
            short = (t[:32] + "…") if len(t) > 32 else t
            self.bridge.kicadStatusChanged.emit(f"snapped to {short}")
        else:
            self.bridge.kicadStatusChanged.emit("KiCad not detected — floating on primary screen")

    def _reposition(self) -> None:
        rect = self._kicad_rect
        if rect is None:
            screen = QGuiApplication.primaryScreen()
            if screen is None:
                return
            rect = screen.availableGeometry()

        margin = 12
        dock_reserve = 150  # room above the bottom dock so the chat panel doesn't cover it

        # -- Chat panel: hug right edge, top-aligned, can't exceed KiCad bounds
        max_chat_w = max(220, rect.width() - 2 * margin)
        max_chat_h = max(240, rect.height() - dock_reserve - 2 * margin)
        chat_w = min(380, max_chat_w)
        chat_h = min(620, max_chat_h)
        chat_x = rect.right() - chat_w - margin
        chat_y = rect.top() + margin
        # Never let the chat panel spill past the left edge.
        if chat_x < rect.left() + margin:
            chat_x = rect.left() + margin
            chat_w = max(220, rect.width() - 2 * margin)
        self.chat.setGeometry(chat_x, chat_y, chat_w, chat_h)

        # -- Dock: centered along the bottom, capped to KiCad width.
        # The Qt window is permanently 300px tall so custom dropdowns have
        # room to render above the pill without any geometry change (which
        # forced a Chromium repaint on every open/close = visible flicker).
        # The transparent 170px above the pill is masked out of the input
        # region so KiCad receives those clicks — see `_apply_dock_mask`.
        max_dock_w = max(320, rect.width() - 2 * margin)
        dock_w = min(720, max_dock_w)
        dock_h = 300
        dock_x = rect.left() + (rect.width() - dock_w) // 2
        dock_y = rect.bottom() - dock_h - margin
        # Never let the dock spill past the top of KiCad's viewport.
        if dock_y < rect.top() + margin:
            dock_y = rect.top() + margin
        self.dock.setGeometry(dock_x, dock_y, dock_w, dock_h)
        self._apply_dock_mask()

    def _apply_dock_mask(self) -> None:
        """Restrict Qt input to the pill (bottom) or the full window when a
        dropdown is open. The transparent top area otherwise steals clicks
        from KiCad. Called on layout + whenever set_dock_expanded flips."""
        w = self.dock.width()
        h = self.dock.height()
        if getattr(self, "_dock_expanded", False):
            self.dock.setMask(QRegion(0, 0, w, h))
        else:
            pill_h = 130
            self.dock.setMask(QRegion(0, h - pill_h, w, pill_h))

    # ---------------- dock init / state ----------------

    def push_dock_init(self) -> None:
        # Match the fallback logic in _on_kicad_changed: schematic file if one
        # is open, else the project name parsed from the current KiCad title.
        # Without this, PCB-only sessions saw the JS placeholder "detecting…"
        # because the watcher's targetChanged emit fires before the bridge is
        # ready, and later polls don't re-emit unless the title changes.
        target = (
            self._schematic_path.name if self._schematic_path
            else _project_from_title(self._kicad_title) or None
        )
        self.bridge.dockInit.emit({
            "mode": self.mode,
            "auto_erc": self.auto_erc,
            "target_name": target,
            "models": list(agent.MODEL_CANDIDATES),
            "current_model": agent._working_model or agent._choose_model(),
        })

    def set_forced_model(self, name: str) -> None:
        # Force a specific model for the next turn -- clear failed set for it.
        self._forced_model = name
        agent._failed_models.discard(name)
        agent._working_model = name

    def _set_clarification(self, active: bool) -> None:
        if active == self._pending_clarification:
            return
        self._pending_clarification = active
        self.bridge.clarificationChanged.emit(active)

    def set_dock_expanded(self, expanded: bool) -> None:
        # No geometry change — the Qt window stays at its permanent 300px.
        # Only the input mask flips: pill-only when idle so KiCad receives
        # clicks in the transparent top area, whole window when a dropdown
        # is open so its backdrop can catch outside-clicks.
        self._dock_expanded = expanded
        self._apply_dock_mask()

    # ---------------- auth ----------------

    def _auth_state_payload(self) -> Dict[str, Any]:
        """Snapshot the UI reads. Kept small: no JWT ever crosses the bridge."""
        # Always include the account URL so the JS "Account & usage" button
        # can fall back to window.open when the bridge slot fails to fire
        # (race between button-click and QWebChannel slot registration).
        account_url = f"{tracer_auth.web_url()}/billing"
        if self._auth_flow is not None:
            return {
                "state": "pending",
                "user_code": (self._auth_pending or {}).get("user_code", ""),
                "verification_uri": (self._auth_pending or {}).get("verification_uri", ""),
                "verification_uri_complete": (self._auth_pending or {}).get(
                    "verification_uri_complete", ""),
                "account_url": account_url,
            }
        if self._auth_token and self._auth_token.get("access_token"):
            return {
                "state": "logged_in",
                "account_url": account_url,
                # No token in the payload — the JS side never needs to see it.
                # If we later show account details we can re-decode server-side.
            }
        return {"state": "logged_out", "account_url": account_url}

    def push_auth_state(self) -> None:
        self.bridge.authStateChanged.emit(self._auth_state_payload())

    def start_login(self) -> None:
        if self._auth_flow is not None:
            self._add_activity("Sys", "[auth] already logging in")
            return
        self._add_activity("Sys", f"[auth] contacting {tracer_auth.web_url()} …")

        # Qt-thread-safe callbacks — DeviceFlow runs on its own daemon
        # thread; we route each event through the bridge signal which
        # Qt marshals back to the main thread.
        def on_pending(info: Dict[str, Any]) -> None:
            self._auth_pending = info
            self._add_activity(
                "Sys", f"[auth] paste code {info.get('user_code', '?')} in browser")
            self.push_auth_state()

        def on_success(token: Dict[str, Any]) -> None:
            self._auth_token = token
            self._auth_flow = None
            self._auth_pending = None
            self._add_activity("Obs", "[auth] logged in")
            self.push_auth_state()

        def on_error(msg: str) -> None:
            self._auth_flow = None
            self._auth_pending = None
            self._add_activity("Err", f"[auth] {msg}")
            self.push_auth_state()

        self._auth_flow = tracer_auth.DeviceFlow(
            on_pending=on_pending, on_success=on_success, on_error=on_error,
        )
        self._auth_flow.start()
        self.push_auth_state()

    def cancel_login(self) -> None:
        if self._auth_flow is not None:
            self._auth_flow.cancel()
            self._auth_flow = None
            self._auth_pending = None
            self._add_activity("Sys", "[auth] login cancelled")
            self.push_auth_state()

    def logout(self) -> None:
        self.cancel_login()
        tracer_auth.clear_token()
        self._auth_token = None
        self._add_activity("Sys", "[auth] logged out")
        self.push_auth_state()

    def open_account_page(self) -> None:
        import webbrowser
        url = f"{tracer_auth.web_url()}/billing"
        print(f"[account] open_account_page url={url}", file=sys.stderr, flush=True)
        try:
            opened = webbrowser.open(url)
            print(f"[account] webbrowser.open returned {opened}", file=sys.stderr, flush=True)
            self._add_activity("Sys", f"[account] opened {url}")
        except Exception as exc:  # noqa: BLE001
            print(f"[account] webbrowser.open raised: {exc}", file=sys.stderr, flush=True)
            self._add_activity("Err", f"[account] could not open browser: {exc}")

    # ---------------- attachments ----------------

    def _push_attachment(self, path: Path) -> None:
        if len(self._pending_attachments) >= self._MAX_ATTACHMENTS:
            self._add_activity("Sys",
                f"attach limit ({self._MAX_ATTACHMENTS}) reached — remove one first")
            return
        self._pending_attachments.append({"name": path.name, "path": str(path)})
        self._emit_attachments()

    def _emit_attachments(self) -> None:
        self.bridge.attachmentsChanged.emit(list(self._pending_attachments))

    def pick_attachment_file(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self.dock, "Attach a reference image",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp);;All Files (*)")
        if not path_str:
            return
        p = Path(path_str)
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            self._add_activity("Err",
                f"attach: unsupported type {p.suffix} — images only (PNG/JPG/WEBP)")
            return
        self._push_attachment(p)
        self._add_activity("Sys", f"attached: {p.name}")

    def capture_kicad_screenshot(self) -> None:
        # Grab the KiCad main window. Falls back to primary screen if the
        # HWND is unknown or grabbing fails.
        import tempfile
        from PySide6.QtGui import QPixmap
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self._add_activity("Err", "screenshot: no screen available")
            return
        target_hwnd = next(iter(self._kicad_hwnds), 0) or self._target_kicad_hwnd
        try:
            if target_hwnd:
                pixmap = screen.grabWindow(int(target_hwnd))
            else:
                pixmap = screen.grabWindow(0)
        except Exception as exc:  # noqa: BLE001
            self._add_activity("Err", f"screenshot failed: {exc}")
            return
        if pixmap.isNull():
            self._add_activity("Err", "screenshot: empty capture")
            return
        # Save under scratchpad-style temp with a stable naming pattern.
        tf = tempfile.NamedTemporaryFile(
            prefix="tracer-shot-", suffix=".png", delete=False)
        tf.close()
        pixmap.save(tf.name, "PNG")
        p = Path(tf.name)
        self._push_attachment(p)
        self._add_activity("Sys", f"screenshot: {p.name} ({pixmap.width()}x{pixmap.height()})")

    def remove_attachment(self, index: int) -> None:
        if 0 <= index < len(self._pending_attachments):
            gone = self._pending_attachments.pop(index)
            self._add_activity("Sys", f"removed: {gone['name']}")
            self._emit_attachments()

    def _clear_attachments(self) -> None:
        if not self._pending_attachments:
            return
        self._pending_attachments = []
        self._emit_attachments()

    # ---------------- target ----------------

    def pick_target(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self.dock, "Select KiCad Schematic", str(Path.home()),
            "KiCad Schematics (*.kicad_sch);;All Files (*)")
        if path_str:
            self._schematic_path = Path(path_str)
            self.bridge.targetChanged.emit(self._schematic_path.name)
            self._add_activity("Sys", f"target set: {self._schematic_path.name}")

    # ---------------- conversation ----------------

    def new_conversation(self) -> None:
        if self._gen_thread is not None:
            return
        self._set_clarification(False)
        self._history = []
        self._activity_count = 0
        self._change_count = 0
        self._pending_plan = None
        self._undo_ledger = []
        self._current_prompt = ""
        self.bridge.chatCleared.emit()
        self.bridge.changesFinalized.emit(False)

    def send_prompt(self, prompt: str) -> None:
        prompt = prompt.strip()
        if not prompt:
            return
        # Guard against concurrent runs. Check isRunning() rather than object
        # presence: a finished QThread lingers between its own `finished`
        # signal firing and `_reset_gen/_reset_inj` nulling the reference,
        # and prompts sent in that window shouldn't be rejected. Also
        # self-heal: if the reference is stale (thread already stopped),
        # clear it so subsequent guards don't false-fire.
        busy_gen = self._gen_thread is not None and self._gen_thread.isRunning()
        busy_inj = self._inj_thread is not None and self._inj_thread.isRunning()
        if not busy_gen and self._gen_thread is not None:
            self._reset_gen()
        if not busy_inj and self._inj_thread is not None:
            self._reset_inj()
        if busy_gen or busy_inj:
            self._add_activity("Sys", "already working — cancel first")
            return
        self.bridge.userMessage.emit(prompt)
        self._current_prompt = prompt
        self._add_activity("Sys", f"prompt: {prompt}")
        if self.mode == "debug":
            self._add_activity("Sys",
                f"[debug] mode=debug target={self._schematic_path.name if self._schematic_path else '(none)'} "
                f"model={agent._working_model or agent._choose_model()} auto_erc={self.auto_erc}")
        self.bridge.thinkingChanged.emit(True)

        # Snapshot + clear the attachment queue before spawning the worker —
        # they're consumed by this turn, not carried forward.
        attachments = list(self._pending_attachments)
        if attachments:
            names = ", ".join(a["name"] for a in attachments)
            self._add_activity("Sys", f"attachments: {names}")
            self._clear_attachments()

        # A pending clarification pins the reply to the Agent worker path
        # so answering the LLM's question feeds back into the same JSON
        # handshake — regardless of what mode the dropdown shows.
        if self._pending_clarification:
            self._add_activity("Sys", "[clarification] continuing agent turn")
        elif self.mode in ("ask", "plan"):
            self._start_ask(prompt, kind=self.mode, attachments=attachments)
            return

        self._gen_thread = QThread()
        self._gen_worker = GenerateWorker(prompt, list(self._history),
                                           attachments=attachments)
        self._gen_worker.moveToThread(self._gen_thread)
        self._gen_thread.started.connect(self._gen_worker.run)
        self._gen_worker.log_line.connect(self._on_log_line)
        self._gen_worker.finished.connect(self._on_generate_finished)
        self._gen_worker.failed.connect(self._on_generate_failed)
        self._gen_worker.finished.connect(self._gen_thread.quit)
        self._gen_worker.failed.connect(self._gen_thread.quit)
        self._gen_thread.finished.connect(self._reset_gen)
        self._gen_thread.start()

    def _start_ask(self, prompt: str, kind: str = "ask",
                   attachments: Optional[List[Dict[str, str]]] = None) -> None:
        self._gen_thread = QThread()
        self._gen_worker = AskWorker(prompt, list(self._history), kind=kind,
                                     attachments=attachments or [])
        self._gen_worker.moveToThread(self._gen_thread)
        self._gen_thread.started.connect(self._gen_worker.run)
        self._gen_worker.finished.connect(self._on_ask_finished)
        self._gen_worker.failed.connect(self._on_generate_failed)
        self._gen_worker.finished.connect(self._gen_thread.quit)
        self._gen_worker.failed.connect(self._gen_thread.quit)
        self._gen_thread.finished.connect(self._reset_gen)
        self._gen_thread.start()

    @Slot(str)
    def _on_ask_finished(self, reply: str) -> None:
        self.bridge.thinkingChanged.emit(False)
        # Ask mode is stateless-per-turn: keep history so follow-ups make sense.
        self._history.append({"role": "user", "text": "(ask) "})
        self._history.append({"role": "model", "text": reply})
        self.bridge.assistantMessage.emit({"body": reply, "kind": "ok"})

    def cancel_current(self) -> None:
        if self._gen_worker is not None:
            self._gen_worker.cancel()
            self._add_activity("Sys", "cancel requested")
        # A user-initiated cancel also dismisses any pending clarification.
        self._set_clarification(False)

    # ---------------- worker callbacks ----------------

    @Slot(str)
    def _on_log_line(self, line: str) -> None:
        # Route the engine log line into the activity accordion. Split by
        # simple heuristics into Act / Obs / Err / Sys buckets.
        line = line.rstrip()
        if not line:
            return
        kind, text = self._classify_log(line)
        self._add_activity(kind, text)

    def _classify_log(self, line: str) -> tuple[str, str]:
        s = line.strip()
        low = s.lower()
        if s.startswith("Asking ") or s.startswith("POSTing"):
            return "Act", s
        if s.startswith("[preflight]") or s.startswith("[auto-repair]"):
            return "Sys", s
        if s.startswith("got '") or s.startswith("(answered by"):
            return "Obs", s
        if "reject" in low or "error" in low or "failed" in low:
            return "Err", s
        if s.startswith("---") or s.startswith("==="):
            return "Sys", ""  # skip visual rules
        # Skip the raw JSON dump section
        if s.startswith("{") or s.startswith(" "):
            return "Sys", s if len(s) < 120 else s[:117] + "…"
        return "Sys", s

    def _add_activity(self, kind: str, text: str) -> None:
        if not text:
            return
        self._activity_count += 1
        self.bridge.activityAdded.emit({"kind": kind, "text": text})

    def _add_change(self, kind: str, text: str) -> None:
        self._change_count += 1
        self.bridge.changeAdded.emit({"kind": kind, "text": text,
                                       "n": self._change_count})

    @Slot(dict)
    def _on_generate_finished(self, result: Dict[str, Any]) -> None:
        self._history = result.get("history", self._history)
        status = result.get("status")
        self.bridge.thinkingChanged.emit(False)

        if status == "clarification_needed":
            q = result.get("question") or ""
            self.bridge.assistantMessage.emit({"body": q, "kind": "ask"})
            self._set_clarification(True)
            return
        # Any other terminal status closes an open clarification.
        self._set_clarification(False)

        if status == "backend_rejected":
            msg = (f"Backend rejected at '{result.get('error_stage','?')}':\n"
                   f"{result.get('error_detail','?')}")
            self.bridge.assistantMessage.emit({"body": msg, "kind": "error"})
            self._add_activity("Err", msg.split("\n")[0])
            return

        if status != "success":
            self.bridge.assistantMessage.emit({"body": f"status: {status}", "kind": "info"})
            return

        circuit = result.get("circuit") or {}
        backend = result.get("backend") or {}
        schematic_path = backend.get("schematic_path")
        if backend.get("status") not in ("ok", "warning") or not schematic_path:
            self.bridge.assistantMessage.emit({"body": "Backend build failed.", "kind": "error"})
            return

        # Success -- announce and record changes.
        name = circuit.get("name", "circuit")
        stats = backend.get("stats") or {}
        if self.mode == "debug":
            comps = circuit.get("components") or []
            nets = circuit.get("nets") or []
            self._add_activity("Sys",
                f"[debug] circuit={name} comps={len(comps)} nets={len(nets)} "
                f"backend={backend.get('status')} erc.ran={erc.get('ran')} erc.clean={erc.get('clean')}")
            for c in comps[:8]:
                self._add_activity("Sys",
                    f"[debug] {c.get('ref','?')}={c.get('value','')} "
                    f"sym={c.get('symbol','?')} fp={c.get('footprint') or '(none)'}")
        erc = backend.get("erc") or {}
        erc_line = "ERC clean" if (erc.get("ran") and erc.get("clean")) else \
                   f"ERC: {len(erc.get('violations') or [])} issue(s)" if erc.get("ran") else "ERC skipped"
        body = (f"Built {name} — "
                f"{stats.get('components', 0)} components, "
                f"{stats.get('wires', 0)} wires, "
                f"{stats.get('nets', 0)} nets. {erc_line}.\n"
                "Reviewing next steps below.")
        self.bridge.assistantMessage.emit({"body": body, "kind": "ok"})

        # Add per-component change entries so the Changes accordion actually
        # shows what would happen on inject.
        for c in circuit.get("components") or []:
            self._add_change("add",
                f"[add_symbol] {c.get('ref','?')} = {c.get('value','')} ({c.get('symbol','')})")
        for n in circuit.get("nets") or []:
            self._add_change("add",
                f"[add_net] {n.get('name','?')} — {len(n.get('connections') or [])} pins")

        # Stash the plan for the Keep/Discard buttons.
        self._pending_plan = {
            "schematic_path": schematic_path,
            "backend": backend,
            "circuit": circuit,
            "prompt": self._current_prompt,
            "mode": self.mode,
        }

        if self._schematic_path is None:
            self._add_activity("Sys",
                "no target set — pick a .kicad_sch to enable inject")
            self.bridge.changesFinalized.emit(True)  # still show Keep/Discard buttons
        elif self.auto_erc and erc.get("ran") and erc.get("clean"):
            # Auto-inject when ERC is clean and Auto-ERC is on.
            self._start_inject()
        else:
            # Show Keep/Discard controls in the Changes accordion.
            self.bridge.changesFinalized.emit(True)

    @Slot(str)
    def _on_generate_failed(self, message: str) -> None:
        self.bridge.thinkingChanged.emit(False)
        self._set_clarification(False)
        first = message.splitlines()[0] if message else "unknown error"
        # Slice I: recognize cloud-auth failure and drop to logged_out so
        # the chat overlay reappears without the user hunting for a Logout
        # button. tracer_api raises NotAuthenticatedError which formats to
        # "NotAuthenticatedError: <reason>" via the worker's failure emit.
        if "NotAuthenticatedError" in first:
            self._auth_token = None
            self._add_activity("Err", "[auth] token rejected — signing you out")
            self.push_auth_state()
            self.bridge.assistantMessage.emit({
                "body": "Signed out. Please sign in again.",
                "kind": "error",
            })
            return
        if "QuotaExceededError" in first:
            # Message shape from tracer_api: "hobby tier: used 210,000 / 200,000 tokens this month"
            summary = first.split("QuotaExceededError:", 1)[-1].strip()
            self._add_activity("Err", f"[quota] {summary}")
            self.bridge.assistantMessage.emit({
                "body": f"Quota exceeded — {summary}. Open Account (top-right of chat) to upgrade.",
                "kind": "error",
            })
            return
        if first == "cancelled":
            self.bridge.assistantMessage.emit({"body": "Cancelled.", "kind": "info"})
        else:
            # Show the full error in the chat bubble (capped) so backend
            # rejection bodies are diagnosable, not just the topline.
            capped = message if len(message) < 2000 else message[:2000] + "\n… (truncated)"
            self.bridge.assistantMessage.emit({"body": f"✗ {capped}", "kind": "error"})
            self._add_activity("Err", first)
            # Also break the body into per-line Activity entries so the
            # backend's traceback / 500 body is scannable there too.
            for line in message.splitlines()[1:]:
                if line.strip():
                    self._add_activity("Err", line.rstrip()[:400])
        print(message, file=sys.stderr)

    def _reset_gen(self) -> None:
        if self._gen_thread is not None:
            self._gen_thread.deleteLater()
        self._gen_thread = None
        self._gen_worker = None

    # ---------------- Keep / Discard ----------------

    def keep_changes(self) -> None:
        if not self._pending_plan:
            self._add_activity("Sys", "nothing to keep")
            return
        if self._schematic_path is None:
            self._add_activity("Err", "pick a target .kicad_sch first")
            return
        self._start_inject()

    def discard_changes(self) -> None:
        if self._pending_plan:
            self._add_activity("Sys", "plan discarded")
            self._pending_plan = None
        self._change_count = 0
        self.bridge.changesFinalized.emit(False)
        # Clear the JS-side list too
        self.bridge.chatCleared.emit()  # heavy but simplest way to clear the accordion

    # ---------------- injection ----------------

    # ---------------- Undo ledger ----------------

    def _push_undo_snapshot(self, prompt: str) -> None:
        """Copy sch (and pcb if present) to a timestamped .tracer-bak.<ts>."""
        import shutil, datetime
        if self._schematic_path is None:
            return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        entry: Dict[str, Any] = {
            "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": (self._pending_plan_mode() or "agent").upper(),
            "prompt": prompt.strip()[:200],
            "sch_backup": None, "pcb_backup": None,
            "history_len": len(self._history),
        }
        try:
            sch_bak = self._schematic_path.with_suffix(
                f".kicad_sch.tracer-bak.{ts}")
            shutil.copy2(self._schematic_path, sch_bak)
            entry["sch_backup"] = str(sch_bak)
        except OSError as exc:
            self._add_activity("Err", f"[undo] sch snapshot failed: {exc}")
        pcb = self._schematic_path.with_suffix(".kicad_pcb")
        if pcb.is_file():
            try:
                pcb_bak = pcb.with_suffix(f".kicad_pcb.tracer-bak.{ts}")
                shutil.copy2(pcb, pcb_bak)
                entry["pcb_backup"] = str(pcb_bak)
            except OSError as exc:
                self._add_activity("Err", f"[undo] pcb snapshot failed: {exc}")
        self._undo_ledger.append(entry)

    def _pending_plan_mode(self) -> Optional[str]:
        return (self._pending_plan or {}).get("mode") if self._pending_plan else self.mode

    def list_undo_history(self) -> List[Dict[str, Any]]:
        """Return newest-first list for the modal."""
        return [
            {"index": i, "ts": e["ts"], "mode": e["mode"], "prompt": e["prompt"]}
            for i, e in enumerate(self._undo_ledger)
        ][::-1]

    def undo_to(self, index: int) -> Dict[str, Any]:
        """Restore files to the snapshot BEFORE `index`; drop later ledger + history."""
        import shutil
        if index < 0 or index >= len(self._undo_ledger):
            return {"ok": False, "error": "index out of range"}
        entry = self._undo_ledger[index]
        errors: List[str] = []
        # Restore SCH
        sch_bak = entry.get("sch_backup")
        if sch_bak and Path(sch_bak).is_file() and self._schematic_path:
            try:
                shutil.copy2(sch_bak, self._schematic_path)
            except OSError as exc:
                errors.append(f"sch restore: {exc}")
        # Restore PCB
        pcb_bak = entry.get("pcb_backup")
        if pcb_bak and Path(pcb_bak).is_file():
            try:
                pcb = Path(pcb_bak.split(".tracer-bak.")[0])
                shutil.copy2(pcb_bak, pcb)
            except OSError as exc:
                errors.append(f"pcb restore: {exc}")
        # Drop later ledger entries and trim conversation history
        dropped = len(self._undo_ledger) - index
        self._undo_ledger = self._undo_ledger[:index]
        keep = entry.get("history_len") or 0
        self._history = self._history[:keep]
        # Clear UI chat + changes; user gets a fresh slate at the restore point
        self.bridge.chatCleared.emit()
        self.bridge.changesFinalized.emit(False)
        self._add_activity("Sys",
            f"[undo] restored to {entry['ts']} — dropped {dropped} inject(s)")
        for e in errors:
            self._add_activity("Err", f"[undo] {e}")
        # Nudge KiCad to reload the reverted schematic
        send_reload_to_kicad()
        return {"ok": not errors, "errors": errors}

    def _start_inject(self) -> None:
        plan = self._pending_plan
        if not plan or self._schematic_path is None:
            return
        self._pending_plan = None
        self.bridge.thinkingChanged.emit(True)
        self._add_activity("Act",
            f"[inject] merging into {self._schematic_path.name}")
        # Snapshot BEFORE inject for the Undo ledger. tracer_inject also
        # writes a single .tracer-bak (latest-only) but we need per-prompt
        # timestamped copies so the user can walk back multiple steps.
        self._push_undo_snapshot(plan.get("prompt") or "(no prompt)")

        self._inj_thread = QThread()
        self._inj_worker = InjectWorker(
            plan["schematic_path"], str(self._schematic_path))
        self._inj_worker.moveToThread(self._inj_thread)
        self._inj_thread.started.connect(self._inj_worker.run)
        self._inj_worker.finished.connect(self._on_inject_finished)
        self._inj_worker.failed.connect(self._on_inject_failed)
        self._inj_worker.finished.connect(self._inj_thread.quit)
        self._inj_worker.failed.connect(self._inj_thread.quit)
        self._inj_thread.finished.connect(self._reset_inj)
        self._inj_thread.start()

    @Slot(dict)
    def _on_inject_finished(self, summary: Dict[str, Any]) -> None:
        self.bridge.thinkingChanged.emit(False)
        added = summary.get("added") or {}
        target = Path(summary.get("target", "?")).name
        for w in summary.get("warnings") or []:
            self._add_activity("Err", w)
        self._add_activity("Obs",
            f"[inject] merged into {target} — "
            f"{added.get('components',0)} comps, "
            f"{added.get('wires',0)} wires, "
            f"{added.get('labels',0)} labels")
        # Try to auto-refresh KiCad's schematic view.
        refreshed = send_reload_to_kicad()
        self._add_activity("Sys",
            "sent Ctrl+F5 to KiCad" if refreshed
            else "KiCad not focused — use File → Revert to reload")
        self.bridge.assistantMessage.emit({
            "body": f"Injected into {target}. Refresh in KiCad if the view didn't update.",
            "kind": "ok"})
        self.bridge.changesFinalized.emit(False)
        # PCB DRC with schematic parity: honest sync check. Sibling .kicad_pcb
        # tells us if the user has a board — if not, silently skip.
        self._start_pcb_drc(summary.get("target"))

    def _start_pcb_drc(self, sch_target: Optional[str]) -> None:
        if not sch_target:
            return
        pcb = Path(sch_target).with_suffix(".kicad_pcb")
        if not pcb.is_file():
            self._add_activity("Sys", "no sibling .kicad_pcb — skipping DRC")
            return
        cli = _find_kicad_cli()
        if not cli:
            self._add_activity("Err", "kicad-cli not found on PATH")
            return
        self._add_activity("Act", f"[drc] {pcb.name} (schematic parity)")
        self._drc_thread = QThread()
        self._drc_worker = PcbDrcWorker(str(pcb), cli)
        self._drc_worker.moveToThread(self._drc_thread)
        self._drc_thread.started.connect(self._drc_worker.run)
        self._drc_worker.done.connect(self._on_pcb_drc_done)
        self._drc_worker.failed.connect(self._on_pcb_drc_failed)
        self._drc_worker.done.connect(self._drc_thread.quit)
        self._drc_worker.failed.connect(self._drc_thread.quit)
        self._drc_thread.finished.connect(self._drc_worker.deleteLater)
        self._drc_thread.finished.connect(self._drc_thread.deleteLater)
        self._drc_thread.start()

    @Slot(dict)
    def _on_pcb_drc_done(self, info: Dict[str, Any]) -> None:
        errs = info.get("errors", 0)
        warns = info.get("warnings", 0)
        parity = info.get("parity_mismatches", 0)
        for v in info.get("violations") or []:
            sev = (v.get("severity") or "?").lower()
            desc = v.get("description") or v.get("type") or "?"
            tag = "Err" if sev == "error" else "Sys"
            self._add_activity(tag, f"[drc:{sev}] {desc}")
        if info.get("ok") and parity == 0:
            self._add_activity("Obs", f"[drc] clean ({warns} warnings)")
        else:
            hint = " — press F8 in PCB Editor to sync" if parity else ""
            self._add_activity("Obs",
                f"[drc] {errs} errors, {warns} warnings"
                f"{f', {parity} parity mismatches' if parity else ''}{hint}")

    @Slot(str)
    def _on_pcb_drc_failed(self, message: str) -> None:
        self._add_activity("Err", f"[drc] {message}")

    @Slot(str)
    def _on_inject_failed(self, message: str) -> None:
        self.bridge.thinkingChanged.emit(False)
        self._add_activity("Err", message)
        self.bridge.assistantMessage.emit({
            "body": f"Injection failed: {message}", "kind": "error"})

    def _reset_inj(self) -> None:
        if self._inj_thread is not None:
            self._inj_thread.deleteLater()
        self._inj_thread = None
        self._inj_worker = None


# ============================================================================
# entry point
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kicad-hwnd", type=int, default=0)
    args = parser.parse_args()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    if not _acquire_instance():
        return 0
    backend = BackendServer()
    backend.start()

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("Tracer")

    shell = OverlayShell(backend)
    shell.show()

    try:
        return qt_app.exec()
    finally:
        backend.stop()


if __name__ == "__main__":
    raise SystemExit(main())
