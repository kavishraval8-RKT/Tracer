"""KiCad window titles — the parser has to survive:
- em-dash, en-dash, and hyphen separators (KiCad changed this across versions)
- dirty-buffer '*' prefix on the project name
- filenames that already include .kicad_sch (KiCad 10 sometimes shows both)
- window that isn't a KiCad editor at all
"""
from tracer_helpers import _project_from_title


def test_em_dash():
    assert _project_from_title("tracertest — PCB Editor") == "tracertest"


def test_en_dash():
    assert _project_from_title("tracertest – Schematic Editor") == "tracertest"


def test_hyphen():
    assert _project_from_title("tracertest - Schematic Editor") == "tracertest"


def test_empty_title():
    assert _project_from_title("") == ""


def test_no_separator_returns_full_title():
    # e.g. KiCad's own project manager window — no dash at all.
    assert _project_from_title("KiCad Project Manager") == "KiCad Project Manager"


def test_dirty_buffer_prefix_kept():
    # A leading '*' is a KiCad "unsaved changes" indicator; keep it — the
    # caller strips it explicitly. We must not eat it here.
    assert _project_from_title("*mydesign — Schematic Editor") == "*mydesign"


def test_project_name_with_spaces():
    assert _project_from_title("my design — Schematic Editor") == "my design"


def test_only_first_separator_splits():
    # If the project name itself contains " - ", the FIRST separator wins.
    assert _project_from_title("a - b - Schematic Editor") == "a"
