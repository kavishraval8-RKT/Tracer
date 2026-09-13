"""Nuclear deployment script for Tracer Copilot KiCad plugin.

This script performs a complete reset:
1. Eradicates ALL ghost plugin installs from KiCad directories
2. Dynamically resolves KICAD_DOCUMENTS_HOME to find the correct plugin directory
3. Deploys the IPC plugin to $KICAD_DOCUMENTS_HOME/10.0/plugins/tracer_plugin
4. Ensures sidecar_app.py is in the correct location

Usage:
    python deploy.py
    python deploy.py --dry-run
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE_PLUGIN = HERE / "tracer_plugin"
SIDECAR_APP = HERE / "sidecar_app.py"
KICAD_VERSION = "10.0"

# Files to deploy for the IPC plugin
IPC_PLUGIN_FILES = (
    "plugin.json",
    "icon.png",
    "requirements.txt",
    "tracer_ipc.py",
    "sidecar_launcher.py",
    "__init__.py",
    "README.md",
)

# Names of ghost plugin folders to eradicate
GHOST_PLUGIN_NAMES = (
    "tracer_plugin",
    "tracer_engine",
    "tracer_kicad_plugin",
)


def get_target_plugin_dir() -> Path:
    """Dynamically resolve the IPC plugin directory.
    
    KiCad 10 IPC plugins MUST be in KICAD_DOCUMENTS_HOME/version/plugins/
    NOT in the installation directory! The source code confirms this in paths.cpp:
    
        wxString PATHS::GetUserPluginsPath()
        {
            wxFileName tmp;
            getUserDocumentPath( tmp );  // Uses KICAD_DOCUMENTS_HOME
            tmp.AppendDir( wxT( "plugins" ) );
            return tmp.GetPath();
        }
    
    Returns:
        Path to the IPC plugin directory
    """
    # CRITICAL: Use KICAD_DOCUMENTS_HOME, NOT the installation directory
    kicad_docs_home = os.environ.get("KICAD_DOCUMENTS_HOME")
    if not kicad_docs_home:
        kicad_docs_home = os.path.expanduser("~/Documents/KiCad")
    
    print(f"  📍 Using KICAD_DOCUMENTS_HOME: {kicad_docs_home}")
    return Path(kicad_docs_home) / KICAD_VERSION / "plugins" / "tracer_plugin"


def get_ghost_directories() -> list[Path]:
    """Return all possible directories where ghost installs might exist.
    
    This includes:
    - Old mistaken %APPDATA% installations
    - KICAD_DOCUMENTS_HOME installations (if different from target)
    - Legacy scripting/plugins directories
    - The target directory itself (for clean reinstall)
    """
    directories = []
    
    # Explicit ghost paths from previous mistaken deployments
    appdata = os.environ.get("APPDATA")
    if appdata:
        directories.append(Path(appdata) / "kicad" / KICAD_VERSION / "plugins")
        directories.append(Path(appdata) / "kicad" / KICAD_VERSION / "scripting" / "plugins")
    
    # KICAD_DOCUMENTS_HOME based installations
    kicad_docs_home = os.environ.get("KICAD_DOCUMENTS_HOME")
    if kicad_docs_home:
        directories.append(Path(kicad_docs_home) / KICAD_VERSION / "plugins")
        directories.append(Path(kicad_docs_home) / KICAD_VERSION / "scripting" / "plugins")
    
    # Legacy Documents fallback
    directories.append(Path.home() / "Documents" / "KiCad" / KICAD_VERSION / "plugins")
    directories.append(Path.home() / "Documents" / "KiCad" / KICAD_VERSION / "scripting" / "plugins")
    
    # KiCad installation directories - clean these but not the target
    directories.extend([
        Path(r"C:\KiCad\KiCad") / KICAD_VERSION / "plugins",
        Path(r"C:\KiCad\KiCad") / KICAD_VERSION / "scripting" / "plugins",
    ])
    
    # The target directory itself (for clean reinstall)
    target_parent = get_target_plugin_dir().parent
    if target_parent.exists():
        directories.append(target_parent)
    
    return [d for d in directories if d.exists()]


def eradicate_ghosts(dry_run: bool) -> None:
    """Remove ALL traces of old Tracer plugin installs from every possible location."""
    print("\n🔥 PHASE 1: ERADICATE GHOST INSTALLS")
    print("=" * 70)
    
    ghost_dirs = get_ghost_directories()
    removed_count = 0
    
    for ghost_dir in ghost_dirs:
        for ghost_name in GHOST_PLUGIN_NAMES:
            ghost_path = ghost_dir / ghost_name
            if ghost_path.exists() and ghost_path.is_dir():
                print(f"  🗑️  Removing: {ghost_path}")
                if not dry_run:
                    shutil.rmtree(ghost_path, ignore_errors=True)
                removed_count += 1
    
    if removed_count == 0:
        print("  ✅ No ghost installs found")
    else:
        print(f"  ✅ Removed {removed_count} ghost install(s)")


def deploy_ipc_plugin(dry_run: bool) -> bool:
    """Deploy the IPC plugin to the canonical location."""
    print("\n🔧 PHASE 2: DEPLOY IPC PLUGIN")
    print("=" * 70)
    
    if not SOURCE_PLUGIN.exists():
        print(f"  ❌ ERROR: Source plugin directory not found: {SOURCE_PLUGIN}")
        return False
    
    target_dir = get_target_plugin_dir()
    print(f"  📂 Target: {target_dir}")
    
    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
    
    copied = 0
    updated = 0
    skipped = 0
    
    for filename in IPC_PLUGIN_FILES:
        src_file = SOURCE_PLUGIN / filename
        dst_file = target_dir / filename
        
        if not src_file.exists():
            print(f"  ⚠️  Missing source file: {filename}")
            continue
        
        if dst_file.exists():
            if filecmp.cmp(src_file, dst_file, shallow=False):
                print(f"  ⏭️  Already up-to-date: {filename}")
                skipped += 1
                continue
            else:
                print(f"  🔄 Updating: {filename}")
                updated += 1
        else:
            print(f"  ➕ Copying: {filename}")
            copied += 1
        
        if not dry_run:
            shutil.copy2(src_file, dst_file)
    
    pycache = target_dir / "__pycache__"
    if pycache.exists() and not dry_run:
        shutil.rmtree(pycache, ignore_errors=True)
        print(f"  🧹 Cleaned: __pycache__")
    
    print(f"\n  ✅ Plugin deployed: {copied} new, {updated} updated, {skipped} unchanged")
    return True


def verify_sidecar(dry_run: bool) -> bool:
    """Verify the sidecar app is in the correct location."""
    print("\n🎯 PHASE 3: VERIFY SIDECAR")
    print("=" * 70)
    
    if not SIDECAR_APP.exists():
        print(f"  ❌ ERROR: sidecar_app.py not found: {SIDECAR_APP}")
        return False
    
    print(f"  ✅ Sidecar located: {SIDECAR_APP}")
    
    content = SIDECAR_APP.read_text(encoding="utf-8")
    has_gpu_bypass = "--disable-gpu" in content and "QTWEBENGINE_CHROMIUM_FLAGS" in content
    has_translucent = "WA_TranslucentBackground" in content
    has_frameless = "FramelessWindowHint" in content
    
    if has_gpu_bypass and has_translucent and has_frameless:
        print(f"  ✅ GPU bypass enabled")
        print(f"  ✅ Frameless window configured")
        print(f"  ✅ Translucent background enabled")
    else:
        print(f"  ⚠️  WARNING: Some hardening flags may be missing")
    
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes"
    )
    args = parser.parse_args()
    
    print("╔" + "=" * 68 + "╗")
    print("║  TRACER COPILOT - NUCLEAR RESET & DEPLOYMENT                     ║")
    print("╚" + "=" * 68 + "╝")
    
    if args.dry_run:
        print("\n⚠️  DRY RUN MODE - No changes will be made\n")
    
    eradicate_ghosts(args.dry_run)
    
    if not deploy_ipc_plugin(args.dry_run):
        return 1
    
    if not verify_sidecar(args.dry_run):
        return 1
    
    print("\n" + "=" * 70)
    print("✅ DEPLOYMENT COMPLETE")
    print("=" * 70)
    
    if not args.dry_run:
        print("\n📋 Next steps:")
        print("  1. Fully close KiCad (all windows)")
        print("  2. Restart KiCad")
        print("  3. Open Preferences > Plugins > Refresh Plugins")
        print("  4. You should see ONE puzzle-piece button: 'Tracer Copilot'")
        print("  5. Click it to launch the overlay panels")
        print(f"\n💡 Plugin installed to:")
        print(f"   {get_target_plugin_dir()}")
        print(f"\n💡 Sidecar located at:")
        print(f"   {SIDECAR_APP}")
    else:
        print("\nRun without --dry-run to apply changes.")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
