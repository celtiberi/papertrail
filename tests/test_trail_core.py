from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from papertrail import Trail


@dataclass(frozen=True)
class FrozenSignals:
    intent: str
    score: float


@dataclass
class MutableTurnState:
    user_text: str
    signals: FrozenSignals | None = None
    flags: list[str] = field(default_factory=list)
    route: str = "default"
    reply: str = ""
    _internal_handle: object = field(default_factory=object)


def _helper_update(trail: Trail[MutableTurnState]) -> None:
    trail.set("route", "recast", reason="grammar slip detected", evidence={"score": 0.91})


def test_dataclass_set_decide_note_and_caller() -> None:
    state = MutableTurnState(user_text="hello world")
    trail = Trail(state, name="turn-1", input_summary={"user_text": state.user_text})

    with trail.stage("classify"):
        trail.decide(
            "question_gate",
            outcome="skip",
            reason="question score below threshold",
            evidence={"score": 0.31, "threshold": 0.55},
        )
        _helper_update(trail)
        trail.note("checked accent inventory", evidence={"missing": ["accent_1"]})

    assert state.route == "recast"
    entries = trail.entries
    assert [e.seq for e in entries] == [1, 2, 3]
    assert [e.kind for e in entries] == ["decision", "set", "note"]
    assert entries[0].outcome == "skip"
    assert entries[0].stage == "classify"
    assert "test_trail_core.py:" in entries[0].caller
    assert entries[1].before == "default"
    assert entries[1].after == "recast"
    assert "_helper_update" in entries[1].caller

    assert len(trail.decisions()) == 1
    assert len(trail.for_field("route")) == 1
    assert trail.stages[0].changes == 1
    assert trail.stages[0].decisions == 1
    assert trail.stages[0].status == "ok"


def test_dict_state_and_frozen_record_change() -> None:
    state: dict[str, object] = {"step": "init", "signals": None}
    trail = Trail(state, name="dict-turn")

    sig_v1 = FrozenSignals(intent="drill", score=0.72)
    sig_v2 = FrozenSignals(intent="question", score=0.94)

    with trail.stage("sensing"):
        trail.set("step", "classified", reason="first classifier pass")
        trail.record_change("signals", None, sig_v1, reason="initial segmentation")
        trail.record_change("signals", sig_v1, sig_v2, reason="refined with prosody score")
        state["signals"] = sig_v2

    sig_entries = trail.for_field("signals")
    assert len(sig_entries) == 2
    assert sig_entries[0].before is None
    assert sig_entries[0].after == {"intent": "drill", "score": 0.72}
    assert sig_entries[1].before == {"intent": "drill", "score": 0.72}
    assert sig_entries[1].after == {"intent": "question", "score": 0.94}
    assert state["step"] == "classified"


def test_stage_watch_diffs_direct_mutations_without_double_logging() -> None:
    state = MutableTurnState(user_text="hola")
    trail = Trail(state, name="watch-turn", exclude=("reply",))

    def _legacy_step(st: MutableTurnState) -> None:
        st.flags.append("needs_recast")
        st.reply = "ignored due to exclude"

    with trail.stage("legacy_intake", watch="*"):
        trail.set("route", "legacy", reason="explicit set should not duplicate as diff")
        _legacy_step(state)

    entries = trail.entries
    assert [(e.field, e.kind) for e in entries] == [
        ("route", "set"),
        ("flags", "diff"),
    ]
    assert entries[1].before == []
    assert entries[1].after == ["needs_recast"]
    assert "test_trail_core.py:" in entries[1].caller
    assert entries[1].stage == "legacy_intake"


def test_stage_exception_records_error_entry_and_partial_diffs() -> None:
    state = MutableTurnState(user_text="hola")
    trail = Trail(state, name="error-turn")

    with pytest.raises(RuntimeError, match="model timeout"):
        with trail.stage("reply_call", watch=("route",)):
            state.route = "in_flight"
            raise RuntimeError("model timeout")

    errors = trail.errors()
    assert len(errors) == 1
    assert errors[0].field == "RuntimeError"
    assert errors[0].outcome == "raised"
    assert "model timeout" in errors[0].reason
    assert errors[0].duration_ms is not None
    assert [(e.field, e.kind) for e in trail.entries] == [
        ("route", "diff"),
        ("RuntimeError", "error"),
    ]
    assert trail.stages[0].status == "error"
    assert trail.stages[0].changes == 1
