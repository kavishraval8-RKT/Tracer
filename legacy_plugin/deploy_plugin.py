"""Install Tracer IPC plugin into KiCad 10.

Deploy to ONE canonical root only. Extra copies in other KiCad document
trees cause duplicate toolbar buttons.

    python deploy_plugin.py --clean
"""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "tracer_plugin"

IPC_PAYLOAD = (
    "plugin.json",
    "requirements.txt",
    "tracer_ipc.py",
    "sidecar_launcher.py",
    "README.md",
)

KICAD_VERSION = "10.0"
STALE_SCRIPTING_DIRS = ("tracer_kicad_plugin", "tracer_engine", "tracer_plugin")


def kicad_roots() -> list[Path]:
    seen: set[Path] = set()
    roots: list[Path] = []
    candidates = []
    override = os.environ.get("KICAD_DOCUMENTS_HOME")
    if override:
        candidates.append(Path(override) / "KiCad" / KICAD_VERSION)
    candidates.extend((
        Path(r"C:\KiCad\KiCad") / KICAD_VERSION,
        Path(r"C:\KiCad") / KICAD_VERSION,
        Path.home() / "Documents" / "KiCad" / KICAD_VERSION,
    ))
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def primary_root() -> Path:
    roots = kicad_roots()
    if not roots:
        return Path.home() / "Documents" / "KiCad" / KICAD_VERSION
    return roots[0]


def ipc_target(root: Path) -> Path:
    return root / "plugins" / SOURCE.name


def check_manifest() -> bool:
    manifest = SOURCE / "plugin.json"
    leaf = Path("share") / "kicad" / "schemas" / "api.v1.schema.json"
    bases = [Path(sys.prefix).parent]
    if sys.platform == "win32":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            root = os.environ.get(var)
            if root:
                bases.append(Path(root) / "KiCad" / KICAD_VERSION)
    for base in bases:
        if (base / leaf).is_file():
            schema_path = base / leaf
            break
    else:
        print("  ! KiCad's api.v1.schema.json not found; skipping validation")
        return True
    try:
        import jsonschema
    except ImportError:
        print("  ! jsonschema not installed; skipping validation")
        return True
    errors = sorted(
        jsonschema.Draft7Validator(json.loads(schema_path.read_text(encoding="utf-8")))
        .iter_errors(json.loads(manifest.read_text(encoding="utf-8"))),
        key=lambda e: list(e.path))
    if errors:
        for err in errors:
            print(f"  x plugin.json{list(err.path)}: {err.message}")
        return False
    print("  plugin.json validates against api.v1.schema.json")
    return True


def _copy_files(payload: tuple[str, ...], src_root: Path, dest: Path,
                dry_run: bool) -> None:
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    for name in payload:
        src_file = src_root / name
        dst_file = dest / name
        if not src_file.is_file():
            print(f"  ! missing {name}", file=sys.stderr)
            continue
        if dst_file.is_file() and filecmp.cmp(src_file, dst_file, shallow=False):
            print(f"     = {name}")
            continue
        print(f"     {'+' if not dst_file.exists() else 'u'} {name}")
        if not dry_run:
            shutil.copy2(src_file, dst_file)
    cache = dest / "__pycache__"
    if cache.is_dir() and not dry_run:
        shutil.rmtree(cache, ignore_errors=True)


def _purge_root(root: Path, dry_run: bool) -> None:
    scripting = root / "scripting" / "plugins"
    for name in STALE_SCRIPTING_DIRS:
        stale = scripting / name
        if stale.is_dir():
            print(f"  clean  - {stale}")
            if not dry_run:
                shutil.rmtree(stale, ignore_errors=True)
    stale_ipc = ipc_target(root)
    if stale_ipc.is_dir():
        print(f"  clean  - {stale_ipc}")
        if not dry_run:
            shutil.rmtree(stale_ipc, ignore_errors=True)


def deploy(dry_run: bool, clean: bool) -> int:
    if not SOURCE.is_dir():
        print(f"error: {SOURCE} does not exist", file=sys.stderr)
        return 1

    roots = kicad_roots()
    primary = primary_root()
    print(f"source:  {SOURCE}")
    print(f"primary: {primary}")
    print(f"sidecar: {os.environ.get('TRACER_HOME', r'C:\tracer-engine')}")
    if not check_manifest():
        return 1

    if clean:
        print("\nRemoving ALL Tracer plugin copies")
        for root in roots:
            print(f"  {root}")
            _purge_root(root, dry_run)

    dest = ipc_target(primary)
    print(f"\nDeploy IPC plugin (single copy)\n  -> {dest}")
    _copy_files(IPC_PAYLOAD, SOURCE, dest, dry_run)

    print("\nFully restart KiCad, then Preferences > Plugins > Refresh Plugins"
          if not dry_run else "\n(dry run -- nothing written)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    sys.exit(deploy(args.dry_run, args.clean))
