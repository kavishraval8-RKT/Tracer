"""kicad-cli DRC JSON → PcbDrcWorker.done payload.

The parser has to survive:
- Missing top-level keys (a clean board omits some entirely)
- Missing "severity" on an item (kicad-cli 10 has emitted None)
- Case-insensitive severity ("Error" vs "error")
- Absurdly large violation lists (UI cap at 32)
- schematic_parity treated as violations AND counted separately
"""
from tracer_helpers import parse_drc_json


def test_clean_board_no_keys():
    out = parse_drc_json({})
    assert out == {
        "ok": True,
        "errors": 0,
        "warnings": 0,
        "parity_mismatches": 0,
        "violations": [],
    }


def test_errors_flip_ok_false():
    out = parse_drc_json({
        "violations": [{"severity": "error", "type": "clearance"}],
    })
    assert out["ok"] is False
    assert out["errors"] == 1
    assert out["warnings"] == 0


def test_warnings_do_not_flip_ok():
    out = parse_drc_json({
        "violations": [{"severity": "warning", "type": "silk_over_pad"}],
    })
    assert out["ok"] is True
    assert out["warnings"] == 1
    assert out["errors"] == 0


def test_case_insensitive_severity():
    out = parse_drc_json({
        "violations": [
            {"severity": "Error"},
            {"severity": "ERROR"},
            {"severity": "Warning"},
        ],
    })
    assert out["errors"] == 2
    assert out["warnings"] == 1


def test_missing_severity_counted_as_neither():
    out = parse_drc_json({
        "violations": [{"type": "shrug", "severity": None}, {"type": "shrug"}],
    })
    assert out["errors"] == 0
    assert out["warnings"] == 0
    assert len(out["violations"]) == 2


def test_unconnected_and_parity_folded_in():
    out = parse_drc_json({
        "violations":        [{"severity": "error"}],
        "unconnected_items": [{"severity": "warning"}],
        "schematic_parity":  [{"severity": "error"}, {"severity": "error"}],
    })
    assert out["errors"] == 3
    assert out["warnings"] == 1
    assert out["parity_mismatches"] == 2
    assert len(out["violations"]) == 4


def test_violations_capped_at_32():
    out = parse_drc_json({
        "violations": [{"severity": "warning"}] * 200,
    })
    assert out["warnings"] == 200        # counts are NOT capped
    assert len(out["violations"]) == 32  # UI list IS capped
