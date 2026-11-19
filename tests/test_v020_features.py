from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field

import papertrail
from papertrail import NullTrail, Trail, current_trail


@dataclass(frozen=True)
class FrozenSignals:
    intent: str
    score: float


@dataclass
class TurnState:
    text: str
    signals: FrozenSignals | None = None
    classification: dict[str, object] = field(default_factory=dict)
    route: str = "hold"
    reply: str = ""
    heavy_blob: str = "x" * 500


def _deep_sensor_helper(score: float) -> None:
    if score >= 0.75:
        papertrail.decide(
            "sensor_gate",
            outcome="allow",
            reason="ambient trail works across helpers",
            evidence={"score": score},
        )
        papertrail.set("route", "teach", reason="routed by deep sensor")
    else:
        papertrail.decide("sensor_gate", outcome="skip", reason="below threshold")


def test_ambient_contextvar_and_threadpool_activation() -> None:
    # Safe no-ops when no trail is bound.
    assert current_trail() is None
    assert papertrail.decide("noop", outcome="skip", reason="unset") is None
    assert papertrail.note("noop note") is None
    assert papertrail.set("route", "ignored") is None
    assert papertrail.record_change("signals", None, "x") is None

    state = TurnState(text="hello")
    trail = Trail(state, name="ambient-turn", watch_default=("route",))

    with trail.activate():
        assert current_trail() is trail
        with trail.stage("classify"):
            _deep_sensor_helper(0.91)
            papertrail.note("ambient note recorded")

        def worker(branch_name: str, delay_s: float) -> Trail[dict[str, str]]:
            child = trail.branch(branch_name, state={"out": ""})
            with child.activate():
                with child.stage(branch_name):
                    time.sleep(delay_s)
                    papertrail.decide(f"{branch_name}_gate", outcome="run", reason="worker active")
                    papertrail.set("out", branch_name, reason="done")
            return child

        with trail.stage("parallel"):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(worker, "answer", 0.01)
                f2 = pool.submit(worker, "ack", 0.015)
                c1, c2 = f1.result(), f2.result()
            trail.merge(c1)
            trail.merge(c2)

    assert current_trail() is None
    entries = trail.entries
    assert entries[0].kind == "decision"
    assert entries[0].stage == "classify"
    assert "_deep_sensor_helper" in entries[0].caller
    assert entries[1].field == "route"
    assert entries[1].after == "teach"
    assert entries[2].kind == "note"
    assert [e.branch for e in entries[3:]] == ["answer", "answer", "ack", "ack"]

    timings = trail.stage_timings()
    assert set(timings) == {"classify", "parallel", "answer", "ack"}
    assert all(v >= 0.0 for v in timings.values())


def test_external_watchers_and_key_level_dict_delta() -> None:
    plan: dict[str, object] = {f"k{i}": i for i in range(15)}
    plan["move"] = "show"
    classification = {f"c{i}": i for i in range(15)}
    classification["bucket"] = "ok"

    state = TurnState(text="hi", classification=classification)
    trail = Trail(
        state,
        name="delta-turn",
        watch_default=("classification",),
        watchers={"plan": lambda: plan},
    )

    with trail.stage("update_plan"):
        plan["move"] = "use"
        plan["target"] = "ex.greet"
        del plan["k0"]
        state.classification["bucket"] = "slip"

    diffs = [e for e in trail.entries if e.kind == "diff"]
    assert len(diffs) == 2

    cls_diff = next(e for e in diffs if e.field == "classification")
    assert cls_diff.before == {"bucket": "ok"}
    assert cls_diff.after == {"bucket": "slip"}

    plan_diff = next(e for e in diffs if e.field == "plan")
    assert plan_diff.before == {"move": "show", "k0": 0}
    assert plan_diff.after == {"move": "use", "target": "ex.greet"}

    md = trail.to_markdown()
    assert "bucket: 'ok' -> 'slip'" in md
    assert "move: 'show' -> 'use'" in md
    assert "target: <unset> -> 'ex.greet'" in md
    assert "k0: 0 -> <unset>" in md

    # watch=False suppresses both field watching and external watchers for that stage.
    with trail.stage("muted", watch=False):
        plan["move"] = "mix"
    assert len([e for e in trail.entries if e.kind == "diff"]) == 2


def test_disabled_null_trail_watch_default_and_fast_immutable_skip(monkeypatch: object) -> None:
    # 1. Disabled / NullTrail still executes state writes cleanly with zero entries.
    st_off = TurnState(text="off")
    null_trail = NullTrail(st_off)
    assert null_trail.enabled is False
    with null_trail.activate():
        with null_trail.stage("intake", watch="*"):
            assert papertrail.decide("gate", outcome="run", reason="ignored") is None
            assert null_trail.set("route", "direct") is None
        child = null_trail.branch("sub")
        null_trail.merge(child)
    assert st_off.route == "direct"
    assert null_trail.entries == []
    assert null_trail.stages == []
    assert null_trail.stage_timings() == {}

    # 2. Fast immutable skip + watch_default allowlist avoids snapshotting untouched frozen fields.
    sig = FrozenSignals(intent="drill", score=0.8)
    st_on = TurnState(text="unchanged", signals=sig)

    snap_calls: list[str] = []
    import papertrail.trail as trail_mod

    orig_take_snapshot = trail_mod.take_snapshot

    def counting_snapshot(value: object, **kwargs: object) -> object:
        if isinstance(value, FrozenSignals):
            snap_calls.append(value.intent)
        return orig_take_snapshot(value, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(trail_mod, "take_snapshot", counting_snapshot)  # type: ignore[attr-defined]

    trail = Trail(st_on, name="fast-skip", watch_default=("signals", "route"))
    with trail.stage("noop_stage"):
        st_on.heavy_blob = "mutated outside allowlist"
    # FrozenSignals was identical (old is new), so zero FrozenSignals snapshots ran and zero diffs recorded.
    assert snap_calls == []
    assert trail.entries == []

    # Replacing frozen signals triggers one diff (before + after snapshot).
    with trail.stage("replace_signals"):
        st_on.signals = FrozenSignals(intent="question", score=0.95)
    assert snap_calls == ["drill", "question"]
    assert len(trail.entries) == 1
    assert trail.entries[0].field == "signals"
