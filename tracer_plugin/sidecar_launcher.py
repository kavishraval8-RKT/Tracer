"""Launch the external PySide6 Tracer sidecar from KiCad plugins."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
_SIDECAR_MUTEX = "Local\\TracerEngineSidecar"
_PROBE = (
    "import PySide6.QtWebEngineWidgets, uvicorn, "
    "google.generativeai, kicad_sch_api"
)


def _dialog(message: str) -> None:
    try:
        import wx
        wx.MessageBox(
            message, "Tracer Copilot",
            wx.OK | wx.ICON_ERROR | wx.STAY_ON_TOP)
    except Exception:  # noqa: BLE001
        print(f"[Tracer] {message}", file=sys.stderr, flush=True)


def _user_site_for(py: str) -> str:
    import re
    m = re.search(r"[Pp]ython(\d{2,3})", py)
    ver = m.group(1) if m else ""
    appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    if ver:
        return os.path.join(appdata, "Python", f"Python{ver}", "site-packages")
    return ""


def sidecar_env(python: str) -> dict[str, str]:
    """Env safe for pythonw: strip KiCad's Python vars, inject user-site."""
    env = os.environ.copy()
    for var in (
        "PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE",
        "PYTHONEXECUTABLE", "PYTHONSTARTUP", "PYTHONUSERBASE",
    ):
        env.pop(var, None)
    user_site = _user_site_for(python)
    if user_site and os.path.isdir(user_site):
        env["PYTHONPATH"] = user_site
    return env


def sidecar_path() -> str:
    """Always launch from the tracer-engine repo — never a KiCad copy."""
    configured = os.environ.get("TRACER_HOME")
    candidates: list[str] = []
    if configured:
        candidates.append(os.path.join(configured, "sidecar_app.py"))
    candidates.extend((
        r"C:\tracer-engine\sidecar_app.py",
        os.path.expanduser(r"~\tracer-engine\sidecar_app.py"),
    ))
    return next((path for path in candidates if os.path.isfile(path)), "")


def resolve_python() -> str:
    env = os.environ.get("TRACER_PYTHON", "")
    candidates = [
        env,
        r"C:\Python314\python.exe",
        r"C:\Python313\python.exe",
        r"C:\Python312\python.exe",
    ]
    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            continue
        if Path(candidate).name.lower() not in {"python.exe", "pythonw.exe"}:
            continue
        try:
            result = subprocess.run(
                [candidate, "-c", _PROBE],
                capture_output=True,
                timeout=15,
                env=sidecar_env(candidate),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate
    return ""


def _release_sidecar_mutex() -> None:
    if sys.platform != "win32":
        return
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenMutexW(0x001F0001, False, _SIDECAR_MUTEX)
    if handle:
        kernel32.CloseHandle(handle)


def _stop_running_sidecars() -> None:
    """End stale sidecar processes so a toolbar click always relaunches."""
    if sys.platform != "win32":
        return
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter "
                "\"Name='pythonw.exe' OR Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'sidecar_app.py' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            time.sleep(0.2)
    except (OSError, subprocess.SubprocessError):
        pass
    _release_sidecar_mutex()


def log_dir() -> str:
    sidecar = sidecar_path()
    for candidate in (
        Path(sidecar).parent / "logs" if sidecar else None,
        PLUGIN_DIR.parent / "logs",
    ):
        if candidate is None:
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return str(candidate)
        except OSError:
            continue
    return str(PLUGIN_DIR.parent)


def launch_sidecar() -> None:
    sidecar = sidecar_path()
    python = resolve_python()
    print(
        f"[Tracer] sidecar={sidecar or '(missing)'} python={python or '(missing)'}",
        file=sys.stderr, flush=True,
    )
    if not sidecar or not python:
        _dialog(
            "Tracer sidecar could not be started.\n\n"
            f"TRACER_HOME = {os.environ.get('TRACER_HOME', '(not set)')}\n"
            f"TRACER_PYTHON = {os.environ.get('TRACER_PYTHON', '(not set)')}\n\n"
            "Set TRACER_HOME to C:\\tracer-engine and TRACER_PYTHON to a "
            "Python with PySide6, uvicorn, google-generativeai, and "
            "kicad_sch_api installed.")
        return

    _stop_running_sidecars()
    time.sleep(0.5)

    launcher = python
    if sys.platform == "win32":
        pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.isfile(pythonw):
            launcher = pythonw

    log_path = os.path.join(log_dir(), "sidecar.log")
    env = sidecar_env(python)
    env.setdefault(
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "--disable-gpu --disable-gpu-compositing",
    )
    print(
        f"[Tracer] launcher={launcher}\n"
        f"[Tracer] sidecar={sidecar}\n"
        f"[Tracer] PYTHONPATH={env.get('PYTHONPATH', '(none)')}\n"
        f"[Tracer] log={log_path}",
        file=sys.stderr, flush=True,
    )

    creation = 0
    if sys.platform == "win32":
        creation = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW

    try:
        with open(log_path, "ab") as log_f:
            log_f.write(
                f"\n--- launch {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"sidecar={sidecar} python={launcher} ---\n".encode())
            proc = subprocess.Popen(
                [launcher, sidecar],
                cwd=os.path.dirname(sidecar),
                creationflags=creation,
                close_fds=True,
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
    except OSError as exc:
        _dialog(f"Failed to launch Tracer sidecar:\n\n{exc}")
        return

    # Brief crash check — keep short; a long sleep here freezes KiCad when
    # this module is ever imported from an in-process ActionPlugin.
    for _ in range(10):
        time.sleep(0.05)
        if proc.poll() is not None:
            break
    if proc.poll() is not None:
        try:
            with open(log_path, "rb") as f:
                msg = f.read().decode("utf-8", "replace").strip()
        except OSError:
            msg = ""
        if not msg:
            msg = f"(no output, exit code {proc.returncode})"
        _dialog(
            "Tracer sidecar crashed on startup:\n\n"
            f"launcher: {launcher}\n"
            f"sidecar:  {sidecar}\n"
            f"log:      {log_path}\n\n"
            f"{msg[-3000:]}")
        return

    print(f"[Tracer] sidecar running (pid {proc.pid})", file=sys.stderr, flush=True)
