"""Backend agent pure helpers — _cap_history and _to_contents.

_to_contents is tested only in its attachment-free form because
attachment handling opens PIL images from disk.
"""
import importlib
import os

import pytest


@pytest.fixture
def agent(monkeypatch):
    """Import backend.app.agent with GEMINI_API_KEY set to a dummy so its
    module-level configure() call doesn't blow up in CI. TRACER_HISTORY_CAP
    is set to a known value so the cap test is deterministic."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used")
    monkeypatch.setenv("TRACER_HISTORY_CAP", "12")
    from backend.app import agent as mod
    importlib.reload(mod)
    yield mod


# ---------------------------------------------------------------------------
# _cap_history — the 2.5 roadmap item
# ---------------------------------------------------------------------------

def test_cap_history_passes_through_when_under_cap(agent):
    turns = [{"role": "user", "text": f"t{i}"} for i in range(5)]
    assert agent._cap_history(turns) == turns


def test_cap_history_keeps_last_n(agent):
    turns = [{"role": "user", "text": f"t{i}"} for i in range(20)]
    out = agent._cap_history(turns)
    assert len(out) == 12
    assert out[0]["text"] == "t8"    # first kept
    assert out[-1]["text"] == "t19"  # last kept


def test_cap_history_boundary_equal_to_cap_unchanged(agent):
    turns = [{"role": "user", "text": f"t{i}"} for i in range(12)]
    assert agent._cap_history(turns) == turns


def test_cap_history_zero_or_negative_disables_cap(agent, monkeypatch):
    # HISTORY_CAP_TURNS <= 0 → pass-through (env var mechanic).
    monkeypatch.setenv("TRACER_HISTORY_CAP", "0")
    import importlib
    from backend.app import agent as mod
    importlib.reload(mod)
    turns = [{"role": "user", "text": f"t{i}"} for i in range(100)]
    assert mod._cap_history(turns) == turns


def test_cap_history_empty_history(agent):
    assert agent._cap_history([]) == []


# ---------------------------------------------------------------------------
# _to_contents — shape converter (attachment-free path)
# ---------------------------------------------------------------------------

def test_to_contents_maps_roles():
    from backend.app import agent
    history = [
        {"role": "user",  "text": "hi"},
        {"role": "model", "text": "hello"},
    ]
    out = agent._to_contents(history)
    assert out == [
        {"role": "user",  "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "hello"}]},
    ]


def test_to_contents_unknown_role_becomes_user():
    from backend.app import agent
    out = agent._to_contents([{"role": "system", "text": "note"}])
    assert out[0]["role"] == "user"


def test_to_contents_missing_text_defaults_to_empty():
    from backend.app import agent
    out = agent._to_contents([{"role": "user"}])
    assert out == [{"role": "user", "parts": [{"text": ""}]}]


def test_to_contents_empty_history():
    from backend.app import agent
    assert agent._to_contents([]) == []
