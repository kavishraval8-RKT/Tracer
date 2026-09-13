# ✅ NUCLEAR RESET COMPLETED - 2026-09-13

## Mission Accomplished

The KiCad plugin architecture has been completely rebuilt from scratch. All ghost installs have been eradicated, and the working IPC plugin is now the sole entry point.

## What Was Fixed

### Before (Broken State)
- Ghost installs in multiple KiCad directories
- Broken legacy plugin (tracer_kicad_plugin/) that froze KiCad's UI thread
- Duplicate toolbar buttons causing confusion
- Console errors from unregistered plugins

### After (Clean State)
- Single IPC plugin deployed to %APPDATA%\kicad\10.0\plugins\tracer_plugin
- Zero ghost installs across all KiCad directories
- Subprocess-only launch - no UI thread blocking
- GPU-hardened sidecar with frameless window and translucent background

## Verification Results

### 1. Ghost Install Check ✅
All KiCad plugin directories are clean:
- ✅ C:\KiCad\KiCad\10.0\plugins
- ✅ C:\KiCad\10.0\plugins
- ✅ C:\Users\Kavish\Documents\KiCad\10.0\plugins
- ✅ C:\KiCad\KiCad\10.0\scripting\plugins
- ✅ C:\KiCad\10.0\scripting\plugins
- ✅ C:\Users\Kavish\Documents\KiCad\10.0\scripting\plugins

### 2. IPC Plugin Deployment ✅
Deployed 8 files to: C:\Users\Kavish\AppData\Roaming\kicad\10.0\plugins\tracer_plugin\
- plugin.json (581 bytes)
- tracer_ipc.py (1,367 bytes)
- sidecar_launcher.py (7,247 bytes)
- requirements.txt (1,654 bytes)
- __init__.py (608 bytes)
- README.md (2,773 bytes)
- tracer_action.py (57,865 bytes)
- tracer_inject.py (12,706 bytes)

### 3. Sidecar Location ✅
- ✅ C:\tracer-engine\sidecar_app.py
- ✅ GPU bypass enabled
- ✅ Frameless window configured
- ✅ Translucent background enabled

## How to Use

### First-Time Setup
1. Fully close KiCad (all windows)
2. Restart KiCad
3. Open Preferences > Plugins > Refresh Plugins
4. Look for ONE puzzle-piece icon: "Tracer Copilot"

### Launch the Sidecar
1. Click the "Tracer Copilot" puzzle icon
2. Two overlay panels should appear (chat panel and input dock)
3. Panels snap to KiCad's window edges
4. When KiCad is minimized, panels auto-hide

### Re-deployment
If you need to redeploy after updates:

```bash
python C:\tracer-engine\deploy.py
```

### Dry Run (Preview Only)
```bash
python C:\tracer-engine\deploy.py --dry-run
```

## Files Created/Modified

### New Files
- C:\tracer-engine\deploy.py - Nuclear deployment script
- C:\tracer-engine\tracer_kicad_plugin\DEPRECATED.md - Legacy plugin warning
- C:\tracer-engine\DEPLOYMENT_SUMMARY.md - This file

### Modified Files
- C:\tracer-engine\tracer_kicad_plugin\__init__.py - Neutered with deprecation warning
- C:\tracer-engine\tracer_kicad_plugin\plugin.py - Neutered with deprecation warning

### Deployed Files
- %APPDATA%\kicad\10.0\plugins\tracer_plugin\* - IPC plugin (8 files)

## Troubleshooting

### "No puzzle icon appears"
1. Check KiCad's Python console for errors
2. Verify plugin.json is valid JSON
3. Run: python C:\tracer-engine\deploy.py

### "Sidecar crashes immediately"
1. Check C:\tracer-engine\logs\sidecar.log
2. Verify PySide6, uvicorn, google-generativeai are installed
3. Check TRACER_PYTHON environment variable

### "Multiple puzzle icons appear"
Ghost install detected. Run: python C:\tracer-engine\deploy.py

### "KiCad freezes when clicking the icon"
Legacy plugin is still deployed. Run: python C:\tracer-engine\deploy.py

## Technical Details

### IPC Plugin Architecture
- Uses KiCad 10's out-of-process plugin.json mechanism
- Launches sidecar with subprocess.Popen and DETACHED_PROCESS flags
- No PySide6 imports in the plugin code (runs in KiCad's Python)
- Sidecar runs in system Python with full PySide6/Qt stack

### Subprocess Launch Flags
- DETACHED_PROCESS (0x00000008) - Independent process
- CREATE_NO_WINDOW (0x08000000) - No console popup
- stdout/stderr to log file - Prevents pipe deadlocks
- stdin=DEVNULL - No input needed

### Sidecar Hardening
- GPU bypass: QT_OPENGL=software, --disable-gpu flags
- Frameless window: Qt.FramelessWindowHint
- Always on top: Qt.WindowStaysOnTopHint
- Transparent background: WA_TranslucentBackground
- No focus steal: WA_ShowWithoutActivating
- Minimized detection: Hides when KiCad at (-32000, -32000)

## Success Criteria ✅

- [x] All ghost installs eradicated
- [x] IPC plugin deployed to canonical location
- [x] Sidecar has GPU bypass flags
- [x] Sidecar has frameless window flags
- [x] Subprocess launch uses DETACHED_PROCESS
- [x] Legacy plugin neutered
- [x] Deployment script runs without errors
- [x] Verification passes all checks

---

**Deployed:** 2026-09-13  
**Status:** ✅ OPERATIONAL  
**Plugin ID:** com.tracer.copilot  
**Version:** IPC-only (KiCad 10+)
