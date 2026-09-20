"""Schematic-path resolver — the config-JSON walk part of
KiCadWatcher.find_open_schematic. `exists` is injected so nothing
touches the filesystem.
"""
from pathlib import Path

from tracer_helpers import resolve_sch_from_file_history, resolve_sch_from_recent


def _exists_of(*paths: str):
    """Return an `exists` predicate that answers True only for these paths."""
    allowed = {Path(p) for p in paths}
    return lambda p: p in allowed


# ---------------------------------------------------------------------------
# resolve_sch_from_recent
# ---------------------------------------------------------------------------

def test_recent_kicad9_shape_string_entries():
    # KiCad 8/9: {"system": {"recent_projects": [...]}}
    data = {"system": {"recent_projects": [
        r"C:/proj/tracertest.kicad_pro",
        r"C:/proj/other.kicad_pro",
    ]}}
    hit = resolve_sch_from_recent(
        "tracertest.kicad_sch", data,
        exists=_exists_of(r"C:/proj/tracertest.kicad_sch"),
    )
    assert hit == Path(r"C:/proj/tracertest.kicad_sch")


def test_recent_legacy_shape_string_entries():
    # Older schema: recent_projects at the top level.
    data = {"recent_projects": [r"C:/proj/tracertest.kicad_pro"]}
    hit = resolve_sch_from_recent(
        "tracertest.kicad_sch", data,
        exists=_exists_of(r"C:/proj/tracertest.kicad_sch"),
    )
    assert hit == Path(r"C:/proj/tracertest.kicad_sch")


def test_recent_dict_entries_with_path_key():
    data = {"system": {"recent_projects": [
        {"path": r"C:/proj/tracertest.kicad_pro", "name": "tracertest"},
    ]}}
    hit = resolve_sch_from_recent(
        "tracertest.kicad_sch", data,
        exists=_exists_of(r"C:/proj/tracertest.kicad_sch"),
    )
    assert hit == Path(r"C:/proj/tracertest.kicad_sch")


def test_recent_sibling_lookup_when_project_name_differs():
    # File in the recent_projects list is named X.kicad_pro but we're
    # looking for foo.kicad_sch in the SAME directory (multi-schematic
    # project case).
    data = {"system": {"recent_projects": [r"C:/proj/parent.kicad_pro"]}}
    hit = resolve_sch_from_recent(
        "foo.kicad_sch", data,
        exists=_exists_of(r"C:/proj/foo.kicad_sch"),
    )
    assert hit == Path(r"C:/proj/foo.kicad_sch")


def test_recent_returns_none_when_nothing_exists():
    data = {"system": {"recent_projects": [r"C:/proj/tracertest.kicad_pro"]}}
    hit = resolve_sch_from_recent(
        "tracertest.kicad_sch", data,
        exists=lambda p: False,
    )
    assert hit is None


def test_recent_ignores_garbage_entries():
    # Ints, None, {"file": missing keys} — none of these should crash.
    data = {"system": {"recent_projects": [
        None, 42, [], {"nope": "x"}, "",
        r"C:/proj/tracertest.kicad_pro",  # the one real entry
    ]}}
    hit = resolve_sch_from_recent(
        "tracertest.kicad_sch", data,
        exists=_exists_of(r"C:/proj/tracertest.kicad_sch"),
    )
    assert hit == Path(r"C:/proj/tracertest.kicad_sch")


def test_recent_empty_dict_returns_none():
    assert resolve_sch_from_recent("x.kicad_sch", {}) is None


# ---------------------------------------------------------------------------
# resolve_sch_from_file_history
# ---------------------------------------------------------------------------

def test_history_finds_sibling_kicad_sch():
    # pcbnew.json history holds .kicad_pcb paths — sibling .kicad_sch wins.
    data = {"system": {"file_history": [r"C:/proj/tracertest.kicad_pcb"]}}
    hit = resolve_sch_from_file_history(
        "tracertest.kicad_sch", data,
        exists=_exists_of(r"C:/proj/tracertest.kicad_sch"),
    )
    assert hit == Path(r"C:/proj/tracertest.kicad_sch")


def test_history_direct_kicad_sch_entry():
    # eeschema.json history holds .kicad_sch paths directly.
    data = {"system": {"file_history": [r"C:/proj/tracertest.kicad_sch"]}}
    hit = resolve_sch_from_file_history(
        "tracertest.kicad_sch", data,
        exists=_exists_of(r"C:/proj/tracertest.kicad_sch"),
    )
    assert hit == Path(r"C:/proj/tracertest.kicad_sch")


def test_history_first_match_wins_when_multiple_projects_share_a_name():
    data = {"system": {"file_history": [
        r"C:/a/tracertest.kicad_pcb",
        r"C:/b/tracertest.kicad_pcb",
    ]}}
    hit = resolve_sch_from_file_history(
        "tracertest.kicad_sch", data,
        exists=_exists_of(
            r"C:/a/tracertest.kicad_sch", r"C:/b/tracertest.kicad_sch"
        ),
    )
    assert hit == Path(r"C:/a/tracertest.kicad_sch")


def test_history_returns_none_when_nothing_exists():
    data = {"system": {"file_history": [r"C:/proj/tracertest.kicad_pcb"]}}
    assert resolve_sch_from_file_history(
        "tracertest.kicad_sch", data, exists=lambda p: False,
    ) is None
