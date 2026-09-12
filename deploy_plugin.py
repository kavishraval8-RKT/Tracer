"""Install tracer_plugin into both of KiCad's plugin directories.

Tracer has two front doors and KiCad scans for them in two unrelated places,
so a single codebase still has to land twice:

  * the legacy SWIG ActionPlugin, loaded in-process by pcbnew from
        <documents>/scripting/plugins/tracer_plugin/
  * the KiCad 10 IPC API plugin, launched out-of-process from
        <documents>/plugins/tracer_plugin/

`<documents>` is NOT "My Documents" when KICAD_DOCUMENTS_HOME is set, so both
paths are derived from that variable rather than hardcoded. Get this wrong and
KiCad silently scans a directory your plugin is not in, which looks exactly
like a manifest bug. Run with --dry-run to see where this resolves for you.

    python deploy_plugin.py            # copy to both, report what moved
    python deploy_plugin.py --dry-run  # show what would happen
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

#: Everything KiCad needs. `requirements.txt` is not optional: the IPC plugin
#: manager refuses to mark a Python plugin ready without it.
PAYLOAD = (
    "__init__.py",
    "plugin.json",
    "requirements.txt",
    "tracer_action.py",
    "tracer_inject.py",
    "tracer_ipc.py",
    "README.md",
)

KICAD_VERSION = "10.0"


def documents_home() -> Path:
    """KiCad's documents root, honouring KICAD_DOCUMENTS_HOME.

    KiCad appends "KiCad/<version>" to whatever this points at; when it is
    unset it falls back to the user's Documents folder.
    """
    override = os.environ.get("KICAD_DOCUMENTS_HOME")
    if override:
        return Path(override) / "KiCad" / KICAD_VERSION
    return Path.home() / "Documents" / "KiCad" / KICAD_VERSION


def targets() -> dict[str, Path]:
    base = documents_home()
    return {
        "IPC API (Schematic + PCB editors)": base / "plugins" / SOURCE.name,
        "legacy ActionPlugin (PCB editor)": base / "scripting" / "plugins" / SOURCE.name,
    }


def check_manifest() -> bool:
    """Validate plugin.json against KiCad's own schema when it is available."""
    manifest = SOURCE / "plugin.json"
    leaf = Path("share") / "kicad" / "schemas" / "api.v1.schema.json"
    bases = [Path(sys.prefix).parent]
    if sys.platform == "win32":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            root = os.environ.get(var)
            if root:
                bases.append(Path(root) / "KiCad" / KICAD_VERSION)
    elif sys.platform == "darwin":
        bases.append(Path("/Applications/KiCad/KiCad.app/Contents"))
    else:
        bases += [Path("/usr"), Path("/usr/local")]

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


def deploy(dry_run: bool) -> int:
    if not SOURCE.is_dir():
        print(f"error: {SOURCE} does not exist", file=sys.stderr)
        return 1

    missing = [n for n in PAYLOAD if not (SOURCE / n).is_file()]
    if missing:
        print(f"error: missing from {SOURCE}: {', '.join(missing)}", file=sys.stderr)
        return 1

    print(f"source: {SOURCE}")
    if not check_manifest():
        return 1

    for label, dest in targets().items():
        print(f"\n{label}\n  -> {dest}")
        if not dry_run:
            dest.mkdir(parents=True, exist_ok=True)
        for name in PAYLOAD:
            src_file = SOURCE / name
            dst_file = dest / name
            if dst_file.is_file() and filecmp.cmp(src_file, dst_file, shallow=False):
                print(f"     = {name}")
                continue
            print(f"     {'+' if not dst_file.exists() else 'u'} {name}")
            if not dry_run:
                shutil.copy2(src_file, dst_file)
        # A stale __pycache__ can shadow a module KiCad has just been told to
        # reload, so clear it rather than trusting mtime comparison.
        cache = dest / "__pycache__"
        if cache.is_dir() and not dry_run:
            shutil.rmtree(cache, ignore_errors=True)

    print("\nNow: KiCad > Preferences > Plugins > Refresh Plugins"
          if not dry_run else "\n(dry run -- nothing written)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be copied, write nothing")
    sys.exit(deploy(parser.parse_args().dry_run))
