"""Pure, stdlib-only helpers extracted from sidecar_app.py.

Kept here so tests can import them without pulling in PySide6, pywin32,
or google-generativeai. sidecar_app.py re-exports these so nothing
outside this file needs to change its import paths.

The extractions match phase-2.1 targets in docs/ROADMAP.md:
- _project_from_title       (window-title parser)
- resolve_sch_from_recent   (was inline inside find_open_schematic)
- parse_drc_json            (was inline inside PcbDrcWorker.run)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Window-title parser
# ---------------------------------------------------------------------------

def _project_from_title(title: str) -> str:
    """'tracertest — PCB Editor' -> 'tracertest'. Empty title -> ''.

    KiCad titles put the project name first, then a separator, then the
    editor name. Separator has drifted across KiCad versions between
    em-dash, en-dash, and hyphen — we try all three.
    """
    if not title:
        return ""
    for sep in (" — ", " – ", " - "):  # em-dash, en-dash, hyphen
        if sep in title:
            return title.split(sep, 1)[0].strip()
    return title.strip()


# ---------------------------------------------------------------------------
# Schematic-path resolver (from parsed kicad_common.json / editor-history JSON)
# ---------------------------------------------------------------------------

def resolve_sch_from_recent(
    fname: str,
    data: Dict[str, Any],
    *,
    exists: Optional[callable] = None,
) -> Optional[Path]:
    """Given a KiCad config dict (kicad_common.json shape) and a target
    .kicad_sch filename, return the Path if one of the recent projects
    contains it.

    `exists` is a filesystem probe — defaults to Path.is_file. Tests
    inject a fake to avoid touching disk.

    Recognizes two dict shapes:
    - {"system": {"recent_projects": [...]}}  (KiCad 8/9)
    - {"recent_projects": [...]}              (older / legacy)

    Entries in recent_projects can be strings (absolute paths) or dicts
    with a "path" or "file" key.
    """
    _exists = exists or (lambda p: p.is_file())
    recent = (data.get("system") or {}).get("recent_projects") \
        or data.get("recent_projects") \
        or []
    for entry in recent:
        if isinstance(entry, str):
            proj_path = entry
        elif isinstance(entry, dict):
            proj_path = entry.get("path") or entry.get("file") or ""
        else:
            continue
        if not proj_path:
            continue
        pp = Path(proj_path)
        sch = pp.with_suffix(".kicad_sch")
        if sch.name == fname and _exists(sch):
            return sch
        sch_in_dir = pp.parent / fname
        if _exists(sch_in_dir):
            return sch_in_dir
    return None


def resolve_sch_from_file_history(
    fname: str,
    data: Dict[str, Any],
    *,
    exists: Optional[callable] = None,
) -> Optional[Path]:
    """eeschema.json / pcbnew.json / kicad.json shape:
        {"system": {"file_history": ["C:/…/proj.kicad_pro", ...]}}

    Returns the first entry whose sibling .kicad_sch matches fname.
    """
    _exists = exists or (lambda p: p.is_file())
    hist = (data.get("system") or {}).get("file_history") or []
    for entry in hist:
        if not isinstance(entry, str) or not entry:
            continue
        pp = Path(entry)
        sch = pp.with_suffix(".kicad_sch")
        if sch.name == fname and _exists(sch):
            return sch
    return None


# ---------------------------------------------------------------------------
# kicad-cli DRC JSON parser
# ---------------------------------------------------------------------------

def parse_drc_json(data: Dict[str, Any]) -> Dict[str, Any]:
    """KiCad 10 DRC JSON → summary dict emitted by PcbDrcWorker.done.

    Input shape:
      {
        "violations":       [{"severity": "error"|"warning", "type": "...", ...}, ...],
        "unconnected_items":[{...}],
        "schematic_parity": [{...}],
      }

    Output shape:
      {
        "ok":                bool,       # errors == 0
        "errors":            int,
        "warnings":          int,
        "parity_mismatches": int,
        "violations":        list,       # capped at 32 for UI
      }
    """
    v = list(data.get("violations") or [])
    v += list(data.get("unconnected_items") or [])
    parity = list(data.get("schematic_parity") or [])
    v += parity
    errors = sum(1 for x in v if (x.get("severity") or "").lower() == "error")
    warnings = sum(1 for x in v if (x.get("severity") or "").lower() == "warning")
    return {
        "ok": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "parity_mismatches": len(parity),
        "violations": v[:32],
    }
