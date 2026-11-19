"""Core per-turn Trail ledger recording state changes, decisions, branches, and timings."""

from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from .formatters import format_markdown
from .snapshot import (
    dict_delta,
    is_fast_immutable,
    list_watch_fields,
    read_field,
    take_snapshot,
    write_field,
)

T = TypeVar("T")

_PACKAGE_DIR = str(Path(__file__).resolve().parent)
_CONTEXTLIB_FILE = str(Path(contextlib.__file__).resolve())
_SAME = object()
_UNSET = object()

_ACTIVE_TRAIL: ContextVar[Trail[Any] | None] = ContextVar("papertrail_active_trail", default=None)


def _capture_caller(depth: int = 1) -> str:
    """Return 'filename.py:lineno (func)' for the first frame outside papertrail/contextlib."""
    try:
        frame = sys._getframe(depth)
    except (AttributeError, ValueError):
        return ""

    hops = 0
    while frame is not None and hops < 16:
        filename = frame.f_code.co_filename
        if not filename.startswith(_PACKAGE_DIR) and filename != _CONTEXTLIB_FILE:
            return f"{Path(filename).name}:{frame.f_lineno} ({frame.f_code.co_name})"
        frame = frame.f_back
        hops += 1
    return ""


@dataclass(frozen=True, slots=True)
class Entry:
    """One recorded state transition, decision, note, or stage error."""

    seq: int
    at_ms: float
    kind: str
    stage: str
    caller: str
    branch: str | None = None
    field: str | None = None
    before: Any = None
    after: Any = None
    outcome: Any = None
    reason: str = ""
    evidence: dict[str, Any] | None = None
    duration_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StageSummary:
    """Timing and activity summary for one stage execution."""

    stage: str
    branch: str | None = None
    duration_ms: float = 0.0
    status: str = "ok"
    changes: int = 0
    decisions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _StageFrame:
    name: str
    explicit_fields: set[str] = field(default_factory=set)
    changes: int = 0
    decisions: int = 0


def _capture_baseline_entry(raw_val: Any, snap_fn: Callable[[Any], Any]) -> tuple[bool, Any, Any]:
    if is_fast_immutable(raw_val):
        return True, raw_val, None
    return False, None, snap_fn(raw_val)


class Trail(Generic[T]):
    """Lightweight per-turn provenance ledger wrapping a pipeline state object."""

    def __init__(
        self,
        state: T = None,  # type: ignore[assignment]
        *,
        name: str = "",
        input_summary: Any = None,
        exclude: Sequence[str] = (),
        watch_default: str | Sequence[str] | None = None,
        watchers: Mapping[str, Callable[[], Any]] | None = None,
        enabled: bool = True,
        max_str: int = 300,
        redact: Callable[[str], str] | None = None,
        _branch: str | None = None,
        _default_stage: str = "root",
        _t0: float | None = None,
    ) -> None:
        self.state: T = state
        self.name = name
        self.enabled = bool(enabled)
        self.exclude: tuple[str, ...] = tuple(str(x) for x in exclude)
        self.watch_default: str | tuple[str, ...] | None = (
            watch_default
            if watch_default is None or isinstance(watch_default, str)
            else tuple(str(x) for x in watch_default)
        )
        self._watchers: dict[str, Callable[[], Any]] = dict(watchers or {})
        self.max_str = max_str
        self.redact = redact
        self._branch = _branch
        self._default_stage = _default_stage
        self._t0 = time.perf_counter() if _t0 is None else _t0
        self._lock = threading.Lock()
        self._stage_var: ContextVar[tuple[_StageFrame, ...]] = ContextVar(
            f"papertrail_stage_{id(self)}",
            default=(),
        )
        self._entries: list[Entry] = []
        self._stages: list[StageSummary] = []
        self.input_summary: Any = (
            take_snapshot(input_summary, max_str=max_str, redact=redact)
            if self.enabled and input_summary is not None
            else None
        )

    def _elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000.0, 3)

    def _active_frame(self) -> _StageFrame | None:
        stack = self._stage_var.get()
        return stack[-1] if stack else None

    @property
    def current_stage(self) -> str:
        if not self.enabled:
            return self._default_stage
        frame = self._active_frame()
        return frame.name if frame is not None else self._default_stage

    def _snap(self, value: Any) -> Any:
        return take_snapshot(value, max_str=self.max_str, redact=self.redact)

    @contextmanager
    def activate(self) -> Iterator[Trail[T]]:
        """Bind this trail as the active ambient trail in the current ContextVar scope."""
        if not self.enabled:
            yield self
            return
        token = _ACTIVE_TRAIL.set(self)
        try:
            yield self
        finally:
            _ACTIVE_TRAIL.reset(token)

    def _append_entry(
        self,
        *,
        kind: str,
        stage: str | None,
        caller: str,
        field_name: str | None = None,
        before: Any = None,
        after: Any = None,
        outcome: Any = None,
        reason: str = "",
        evidence: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> Entry:
        resolved_stage = stage or self.current_stage
        clean_reason = str(self._snap(reason)) if reason else ""
        clean_evidence = self._snap(evidence) if evidence is not None else None
        if clean_evidence is not None and not isinstance(clean_evidence, dict):
            clean_evidence = {"value": clean_evidence}

        with self._lock:
            entry = Entry(
                seq=len(self._entries) + 1,
                at_ms=self._elapsed_ms(),
                kind=kind,
                stage=resolved_stage,
                caller=caller,
                branch=self._branch,
                field=field_name,
                before=before,
                after=after,
                outcome=outcome,
                reason=clean_reason,
                evidence=clean_evidence,
                duration_ms=duration_ms,
            )
            self._entries.append(entry)
        return entry

    def set(
        self,
        field_name: str,
        value: Any,
        *,
        reason: str = "",
        evidence: dict[str, Any] | None = None,
        stage: str | None = None,
    ) -> Entry | None:
        """Mutate state.<field_name> (or state[field_name]) and record its before/after snapshot."""
        if not self.enabled:
            write_field(self.state, field_name, value)
            return None

        old_val = read_field(self.state, field_name)
        write_field(self.state, field_name, value)
        entry = self._append_entry(
            kind="set",
            stage=stage,
            caller=_capture_caller(2),
            field_name=field_name,
            before=self._snap(old_val),
            after=self._snap(value),
            reason=reason,
            evidence=evidence,
        )
        frame = self._active_frame()
        if frame is not None:
            frame.explicit_fields.add(field_name)
            frame.changes += 1
        return entry

    def record_change(
        self,
        field_name: str,
        before: Any,
        after: Any,
        *,
        reason: str = "",
        evidence: dict[str, Any] | None = None,
        stage: str | None = None,
    ) -> Entry | None:
        """Record an explicit change without mutating (ideal for frozen dataclasses/signals)."""
        if not self.enabled:
            return None

        entry = self._append_entry(
            kind="change",
            stage=stage,
            caller=_capture_caller(2),
            field_name=field_name,
            before=self._snap(before),
            after=self._snap(after),
            reason=reason,
            evidence=evidence,
        )
        frame = self._active_frame()
        if frame is not None:
            frame.explicit_fields.add(field_name)
            frame.changes += 1
        return entry

    def decide(
        self,
        name: str,
        *,
        outcome: Any,
        reason: str = "",
        evidence: dict[str, Any] | None = None,
        stage: str | None = None,
    ) -> Entry | None:
        """Record a branch, gate, threshold, or skip decision with its outcome and evidence."""
        if not self.enabled:
            return None

        entry = self._append_entry(
            kind="decision",
            stage=stage,
            caller=_capture_caller(2),
            field_name=name,
            outcome=self._snap(outcome),
            reason=reason,
            evidence=evidence,
        )
        frame = self._active_frame()
        if frame is not None:
            frame.decisions += 1
        return entry

    def note(
        self,
        reason: str,
        *,
        evidence: dict[str, Any] | None = None,
        stage: str | None = None,
    ) -> Entry | None:
        """Record a free-form provenance observation attached to the current stage."""
        if not self.enabled:
            return None

        return self._append_entry(
            kind="note",
            stage=stage,
            caller=_capture_caller(2),
            reason=reason,
            evidence=evidence,
        )

    def _emit_diff_if_changed(
        self,
        *,
        target_name: str,
        stage_name: str,
        caller: str,
        baseline: tuple[bool, Any, Any],
        after_raw: Any,
        frame: _StageFrame,
    ) -> None:
        was_fast, before_raw, before_snap = baseline
        if was_fast and after_raw is before_raw:
            return
        before_val = self._snap(before_raw) if was_fast else before_snap
        after_val = self._snap(after_raw)
        if before_val == after_val:
            return

        if isinstance(before_val, dict) and isinstance(after_val, dict):
            before_out, after_out = dict_delta(before_val, after_val)
            if not before_out and not after_out:
                return
        else:
            before_out, after_out = before_val, after_val

        self._append_entry(
            kind="diff",
            stage=stage_name,
            caller=caller,
            field_name=target_name,
            before=before_out,
            after=after_out,
        )
        frame.changes += 1

    @contextmanager
    def stage(
        self,
        name: str,
        *,
        watch: str | Sequence[str] | Literal[False] | None | object = _UNSET,
        watchers: Mapping[str, Callable[[], Any]] | None = None,
        exclude: Sequence[str] = (),
    ) -> Iterator[None]:
        """Track timing for a stage and optionally diff state fields and external watchers."""
        if not self.enabled:
            yield
            return

        skip = tuple(dict.fromkeys((*self.exclude, *exclude)))
        skip_set = frozenset(skip)
        if watch is False:
            watch_fields: list[str] = []
            merged_watchers: dict[str, Callable[[], Any]] = {}
        else:
            resolved_watch = self.watch_default if watch is _UNSET else watch
            watch_fields = list_watch_fields(self.state, resolved_watch, skip)  # type: ignore[arg-type]
            merged_watchers = {
                k: fn
                for k, fn in {**self._watchers, **(watchers or {})}.items()
                if k not in skip_set
            }

        field_baseline = {
            f: _capture_baseline_entry(read_field(self.state, f), self._snap)
            for f in watch_fields
        }
        watcher_baseline = {
            w_name: _capture_baseline_entry(w_fn(), self._snap)
            for w_name, w_fn in merged_watchers.items()
        }

        frame = _StageFrame(name=name)
        stack = self._stage_var.get()
        token = self._stage_var.set((*stack, frame))
        started = time.perf_counter()
        status = "ok"
        caught: BaseException | None = None
        caller = _capture_caller(2)

        try:
            yield
        except BaseException as exc:
            status = "error"
            caught = exc
            raise
        finally:
            elapsed = round((time.perf_counter() - started) * 1000.0, 3)
            for field_name in watch_fields:
                if field_name in frame.explicit_fields:
                    continue
                self._emit_diff_if_changed(
                    target_name=field_name,
                    stage_name=name,
                    caller=caller,
                    baseline=field_baseline[field_name],
                    after_raw=read_field(self.state, field_name),
                    frame=frame,
                )

            for w_name, w_fn in merged_watchers.items():
                if w_name in frame.explicit_fields:
                    continue
                self._emit_diff_if_changed(
                    target_name=w_name,
                    stage_name=name,
                    caller=caller,
                    baseline=watcher_baseline[w_name],
                    after_raw=w_fn(),
                    frame=frame,
                )

            if caught is not None:
                self._append_entry(
                    kind="error",
                    stage=name,
                    caller=caller,
                    field_name=type(caught).__name__,
                    outcome="raised",
                    reason=str(caught) or type(caught).__name__,
                    duration_ms=elapsed,
                )

            with self._lock:
                self._stages.append(
                    StageSummary(
                        stage=name,
                        branch=self._branch,
                        duration_ms=elapsed,
                        status=status,
                        changes=frame.changes,
                        decisions=frame.decisions,
                    )
                )
            self._stage_var.reset(token)

    def branch(self, name: str, *, state: Any = _SAME) -> Trail[Any]:
        """Create an isolated child trail sharing the parent clock/redactor for parallel workers."""
        child_state = self.state if state is _SAME else state
        return Trail(
            child_state,
            name=self.name,
            exclude=self.exclude,
            watch_default=self.watch_default,
            watchers=self._watchers if state is _SAME else None,
            enabled=self.enabled,
            max_str=self.max_str,
            redact=self.redact,
            _branch=name,
            _default_stage=self.current_stage if self.current_stage != "root" else name,
            _t0=self._t0,
        )

    def merge(self, child: Trail[Any]) -> None:
        """Merge entries and stage summaries from a child branch with contiguous parent sequences."""
        if not self.enabled or not child.enabled:
            return
        with child._lock:
            child_entries = list(child._entries)
            child_stages = list(child._stages)
            child_branch = child._branch

        with self._lock:
            for item in child_entries:
                self._entries.append(
                    replace(
                        item,
                        seq=len(self._entries) + 1,
                        branch=item.branch or child_branch,
                    )
                )
            self._stages.extend(child_stages)

    @property
    def entries(self) -> list[Entry]:
        with self._lock:
            return list(self._entries)

    @property
    def stages(self) -> list[StageSummary]:
        with self._lock:
            return list(self._stages)

    def stage_timings(self) -> dict[str, float]:
        """Return summed wall-clock duration in milliseconds per stage (keyed 'branch:stage' when branched)."""
        if not self.enabled:
            return {}
        timings: dict[str, float] = {}
        with self._lock:
            for st in self._stages:
                key = f"{st.branch}:{st.stage}" if st.branch and st.branch != st.stage else st.stage
                timings[key] = round(timings.get(key, 0.0) + st.duration_ms, 3)
        return timings

    def for_field(self, field_name: str) -> list[Entry]:
        with self._lock:
            return [e for e in self._entries if e.field == field_name]

    def decisions(self) -> list[Entry]:
        with self._lock:
            return [e for e in self._entries if e.kind == "decision"]

    def errors(self) -> list[Entry]:
        with self._lock:
            return [e for e in self._entries if e.kind == "error"]

    def _build_summary_locked(self) -> dict[str, Any]:
        fields_changed = list(
            dict.fromkeys(e.field for e in self._entries if e.kind in ("set", "change", "diff") and e.field)
        )
        return {
            "entry_count": len(self._entries),
            "change_count": sum(1 for e in self._entries if e.kind in ("set", "change", "diff")),
            "decision_count": sum(1 for e in self._entries if e.kind == "decision"),
            "error_count": sum(1 for e in self._entries if e.kind == "error"),
            "fields_changed": fields_changed,
        }

    def summary(self) -> dict[str, Any]:
        with self._lock:
            out = self._build_summary_locked()
            stages = [s.to_dict() for s in self._stages]
        return {
            "name": self.name,
            "total_ms": self._elapsed_ms() if self.enabled else 0.0,
            **out,
            "stages": stages,
        }

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            entries = [e.to_dict() for e in self._entries]
            stages = [s.to_dict() for s in self._stages]
            summary = self._build_summary_locked()
        return {
            "name": self.name,
            "total_ms": self._elapsed_ms() if self.enabled else 0.0,
            "input_summary": self.input_summary,
            "summary": summary,
            "entries": entries,
            "stages": stages,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_markdown(self, *, include_evidence: bool = True) -> str:
        return format_markdown(self.to_dict(), include_evidence=include_evidence)

    def write_jsonl(self, path: str | Path) -> Path:
        """Append this trail as a single JSON line and return the resolved path."""
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(self.to_dict(), ensure_ascii=False) + "\n"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
        return target


class NullTrail(Trail[T]):
    """Zero-overhead disabled trail suitable as a drop-in default when tracing is off."""

    def __init__(self, state: T = None, **kwargs: Any) -> None:  # type: ignore[assignment]
        kwargs["enabled"] = False
        super().__init__(state, **kwargs)


def current_trail() -> Trail[Any] | None:
    """Return the active Trail bound in the current ContextVar scope, or None."""
    return _ACTIVE_TRAIL.get()


def set(  # noqa: A001
    field_name: str,
    value: Any,
    *,
    reason: str = "",
    evidence: dict[str, Any] | None = None,
    stage: str | None = None,
) -> Entry | None:
    """Mutate and record on the active Trail when set; no-op returning None otherwise."""
    active = _ACTIVE_TRAIL.get()
    if active is None:
        return None
    return active.set(field_name, value, reason=reason, evidence=evidence, stage=stage)


def record_change(
    field_name: str,
    before: Any,
    after: Any,
    *,
    reason: str = "",
    evidence: dict[str, Any] | None = None,
    stage: str | None = None,
) -> Entry | None:
    """Record an explicit change on the active Trail when set; no-op returning None otherwise."""
    active = _ACTIVE_TRAIL.get()
    if active is None or not active.enabled:
        return None
    return active.record_change(field_name, before, after, reason=reason, evidence=evidence, stage=stage)


def decide(
    name: str,
    *,
    outcome: Any,
    reason: str = "",
    evidence: dict[str, Any] | None = None,
    stage: str | None = None,
) -> Entry | None:
    """Record a decision on the active Trail when set; no-op returning None otherwise."""
    active = _ACTIVE_TRAIL.get()
    if active is None or not active.enabled:
        return None
    return active.decide(name, outcome=outcome, reason=reason, evidence=evidence, stage=stage)


def note(
    reason: str,
    *,
    evidence: dict[str, Any] | None = None,
    stage: str | None = None,
) -> Entry | None:
    """Record a note on the active Trail when set; no-op returning None otherwise."""
    active = _ACTIVE_TRAIL.get()
    if active is None or not active.enabled:
        return None
    return active.note(reason, evidence=evidence, stage=stage)
