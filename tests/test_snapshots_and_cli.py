from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import NamedTuple

from papertrail import Trail
from papertrail.cli import main as cli_main
from papertrail.snapshot import REDACTED, take_snapshot


class _Point(NamedTuple):
    x: int
    y: int


class _CustomHandle:
    __slots__ = ("_fd",)

    def __init__(self) -> None:
        self._fd = 7


def test_snapshots_bound_sizes_cycles_secrets_and_handles() -> None:
    cycle: dict[str, object] = {}
    cycle["self"] = cycle

    shared = {"k": "v"}
    payload = {
        "long_str": "a" * 50,
        "long_list": list(range(30)),
        "nested": {"d1": {"d2": {"d3": {"too_deep": 1}}}},
        "cycle": cycle,
        "shared_a": shared,
        "shared_b": shared,
        "point": _Point(3, 4),
        "lock": threading.Lock(),
        "handle": _CustomHandle(),
        "api_key": "sk-live-secret-9999",
        "password": "my-secret-password",
        "note": "xyz_secret_abc token",
        "bad_float": float("nan"),
        "raw_bytes": b"hello world " * 5,
    }

    snap_short_items = take_snapshot(list(range(30)), max_items=5)
    assert snap_short_items == [0, 1, 2, 3, 4, "...[+25 more]"]

    snap = take_snapshot(
        payload,
        max_depth=3,
        max_items=20,
        max_str=12,
        redact=lambda s: s.replace("xyz_secret_abc", "[SCRUBBED]"),
    )
    assert snap["long_str"] == "aaaaaaaaaaaa...[+38 chars]"
    assert snap["long_list"][-1] == "...[+10 more]"
    assert "max_depth" in snap["nested"]["d1"]["d2"]
    assert snap["cycle"]["self"] == "<circular>"
    assert snap["shared_a"] == {"k": "v"}
    assert snap["shared_b"] == {"k": "v"}
    assert snap["point"] == {"x": 3, "y": 4}
    assert snap["lock"].startswith("<")
    assert snap["handle"] == "<_CustomHandle>"
    assert snap["api_key"] == REDACTED
    assert snap["password"] == REDACTED
    assert "[SCRUBBED]" in snap["note"]
    assert "xyz_secret_abc" not in snap["note"]
    assert snap["bad_float"] == "nan"
    assert "[+" in snap["raw_bytes"]

    # Ensure result is cleanly JSON serializable.
    json.dumps(snap)


def test_jsonl_roundtrip_markdown_and_cli_flags(tmp_path: Path, capsys: object) -> None:
    log_path = tmp_path / "turns.jsonl"

    t1 = Trail({"route": "old"}, name="turn-1", input_summary={"msg": "first"})
    with t1.stage("init"):
        t1.set("route", "new", reason="initial route")
    t1.write_jsonl(log_path)

    t2 = Trail({"route": "hold", "reply": ""}, name="turn-2", input_summary={"msg": "second"})
    with t2.stage("classifier"):
        t2.decide("ask_gate", outcome="open", reason="question mark present", evidence={"score": 0.9})
        t2.set("route", "answer", reason="routed to QA")
    with t2.stage("synth"):
        t2.set("reply", "Here is the answer.", reason="prompt=answer_v1")
    t2.write_jsonl(log_path)

    md = t2.to_markdown()
    assert "### Papertrail: turn-2" in md
    assert "`[classifier]`" in md
    assert "**decision** `ask_gate` = `'open'`" in md
    assert "| Stage | Branch | Status | Changes | Decisions | Duration |" in md

    # Default CLI prints latest entry (-1)
    assert cli_main(["show", str(log_path)]) == 0
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "Papertrail: turn-2" in out

    # --name and --index 0 select turn-1
    assert cli_main(["show", str(log_path), "--name", "turn-1", "--index", "0"]) == 0
    out_t1 = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "Papertrail: turn-1" in out_t1

    # --decisions and --json filters entries
    assert cli_main(["show", str(log_path), "--decisions", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert len(parsed["entries"]) == 1
    assert parsed["entries"][0]["field"] == "ask_gate"

    # --field filters to one field
    assert cli_main(["show", str(log_path), "--field", "route"]) == 0
    out_field = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "routed to QA" in out_field
    assert "Here is the answer" not in out_field
