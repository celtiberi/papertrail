from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath
from uuid import UUID

import papertrail
from papertrail import NullTrail, Trail
from papertrail.snapshot import is_fast_immutable, take_snapshot


@dataclass
class _AuditState:
    as_of: datetime.date
    opened_on: datetime.date
    amount: Decimal
    record_id: UUID
    artifact_path: PurePosixPath
    extra: dict[str, object]


class _JsonAwareModel:
    def __init__(self, as_of: datetime.date, amount: Decimal) -> None:
        self.as_of = as_of
        self.amount = amount
        self.modes_called: list[str | None] = []

    def model_dump(self, *, mode: str | None = None) -> dict[str, object]:
        self.modes_called.append(mode)
        if mode == "json":
            return {"as_of": self.as_of.isoformat(), "amount": str(self.amount)}
        return {"as_of": self.as_of, "amount": self.amount}


class _LegacyDumpModel:
    def model_dump(self) -> dict[str, object]:
        return {"when": datetime.date(2026, 5, 1), "cost": Decimal("19.95")}


class _FallbackOnJsonErrorModel:
    def model_dump(self, *, mode: str | None = None) -> dict[str, object]:
        if mode == "json":
            raise ValueError("Unable to serialize custom handle to JSON")
        return {"as_of": datetime.date(2026, 6, 15), "raw": object()}


def test_take_snapshot_preserves_temporal_decimal_uuid_and_path_values() -> None:
    sample_uuid = UUID("12345678-1234-5678-1234-567812345678")
    aware_dt = datetime.datetime(2026, 11, 19, 14, 30, 5, tzinfo=datetime.UTC)
    naive_dt = datetime.datetime(2026, 11, 19, 9, 15, 0)
    payload = {
        "as_of": datetime.date(2026, 11, 19),
        "aware_dt": aware_dt,
        "naive_dt": naive_dt,
        "at_time": datetime.time(8, 45, 30),
        "elapsed": datetime.timedelta(hours=1, minutes=5),
        "amount": Decimal("1234.5600"),
        "record_id": sample_uuid,
        "path": Path("/srv/reports/xyz_secret_abc/summary.json"),
        "posix": PurePosixPath("/data/archive/batch_01.parquet"),
        # Verify scalar leaf types still serialize cleanly right at max_depth.
        "nested": {"d1": {"leaf_date": datetime.date(2026, 1, 2)}},
    }

    snap = take_snapshot(
        payload,
        max_depth=3,
        max_str=34,
        redact=lambda s: s.replace("xyz_secret_abc", "[SCRUBBED]"),
    )
    assert snap["as_of"] == "2026-11-19"
    assert snap["aware_dt"] == "2026-11-19T14:30:05+00:00"
    assert snap["naive_dt"] == "2026-11-19T09:15:00"
    assert snap["at_time"] == "08:45:30"
    assert snap["elapsed"] == "1:05:00"
    assert snap["amount"] == "1234.5600"
    assert snap["record_id"] == "12345678-1234-5678-1234-567812345678"
    assert "[SCRUBBED]" in snap["path"]
    assert "xyz_secret_abc" not in snap["path"]
    assert "...[+" in snap["path"]
    assert snap["posix"] == "/data/archive/batch_01.parquet"
    assert snap["nested"]["d1"]["leaf_date"] == "2026-01-02"
    json.dumps(snap)

    for imm in (
        payload["as_of"],
        aware_dt,
        payload["at_time"],
        payload["elapsed"],
        payload["amount"],
        sample_uuid,
        payload["path"],
        payload["posix"],
    ):
        assert is_fast_immutable(imm)


def test_take_snapshot_model_dump_json_mode_and_fallbacks() -> None:
    aware = _JsonAwareModel(datetime.date(2026, 4, 10), Decimal("42.50"))
    snap_aware = take_snapshot(aware)
    assert aware.modes_called == ["json"]
    assert snap_aware == {"as_of": "2026-04-10", "amount": "42.50"}

    legacy = _LegacyDumpModel()
    assert take_snapshot(legacy) == {"when": "2026-05-01", "cost": "19.95"}

    fallback = _FallbackOnJsonErrorModel()
    assert take_snapshot(fallback) == {"as_of": "2026-06-15", "raw": "<object>"}


def test_trail_stage_diffs_preserve_dates_and_fast_skip_unchanged(monkeypatch: object) -> None:
    import papertrail.trail as trail_mod

    state = _AuditState(
        as_of=datetime.date(2026, 10, 1),
        opened_on=datetime.date(2025, 1, 15),
        amount=Decimal("99.95"),
        record_id=UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        artifact_path=PurePosixPath("/tmp/run.jsonl"),
        extra={"source_as_of": datetime.date(2026, 10, 1), "oldest_source_as_of": datetime.date(2026, 9, 1)},
    )

    snapped_values: list[object] = []
    orig_take_snapshot = trail_mod.take_snapshot

    def spy_snapshot(value: object, **kwargs: object) -> object:
        snapped_values.append(value)
        return orig_take_snapshot(value, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(trail_mod, "take_snapshot", spy_snapshot)  # type: ignore[attr-defined]

    trail = Trail(
        state,
        name="audit-1",
        watch_default=("as_of", "opened_on", "amount", "record_id", "artifact_path", "extra"),
    )
    with trail.stage("freshness"):
        state.as_of = datetime.date(2026, 11, 19)
        state.extra["source_as_of"] = datetime.date(2026, 11, 19)

    diffs = [e for e in trail.entries if e.kind == "diff"]
    diff_by_field = {e.field: e for e in diffs}
    assert set(diff_by_field) == {"as_of", "extra"}
    assert diff_by_field["as_of"].before == "2026-10-01"
    assert diff_by_field["as_of"].after == "2026-11-19"
    assert diff_by_field["extra"].before == {"source_as_of": "2026-10-01"}
    assert diff_by_field["extra"].after == {"source_as_of": "2026-11-19"}

    # Unchanged immutable fields (opened_on, amount, record_id, artifact_path) skipped via identity.
    assert datetime.date(2025, 1, 15) not in snapped_values
    assert Decimal("99.95") not in snapped_values
    assert UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") not in snapped_values
    assert PurePosixPath("/tmp/run.jsonl") not in snapped_values


def test_trail_max_depth_and_max_items_forward_across_snap_input_and_branch() -> None:
    wide_model = {f"field_{i:02d}": i for i in range(25)}
    deep_payload = {"l1": {"l2": {"l3": {"l4": {"leaf": "ok"}}}}}

    trail = Trail(
        {"wide": wide_model, "deep": {}},
        name="wide-run",
        input_summary={"meta": deep_payload, "keys": {f"k{i}": i for i in range(25)}},
        max_depth=6,
        max_items=30,
    )
    assert len(trail.input_summary["keys"]) == 25
    assert "__truncated__" not in trail.input_summary["keys"]
    assert trail.input_summary["meta"]["l1"]["l2"]["l3"]["l4"]["leaf"] == "ok"

    with trail.activate():
        with trail.stage("expand"):
            papertrail.set("deep", deep_payload, reason="deep check")
            papertrail.decide("wide_ok", outcome=True, evidence={"model": wide_model})

    set_entry = next(e for e in trail.entries if e.kind == "set")
    assert set_entry.after["l1"]["l2"]["l3"]["l4"]["leaf"] == "ok"
    dec_entry = trail.decisions()[0]
    assert len(dec_entry.evidence["model"]) == 25  # type: ignore[index]
    assert "__truncated__" not in dec_entry.evidence["model"]  # type: ignore[operator]

    child = trail.branch("worker-1")
    assert child.max_depth == 6
    assert child.max_items == 30
    with child.stage("worker"):
        child.decide("child_wide", outcome=True, evidence={"rows": list(range(25))})
    assert len(child.decisions()[0].evidence["rows"]) == 25  # type: ignore[index]
    trail.merge(child)

    null_trail = NullTrail({"a": 1}, max_depth=6, max_items=30)
    assert null_trail.enabled is False
    assert null_trail.max_depth == 6
    assert null_trail.max_items == 30
