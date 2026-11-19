# Changelog

## 0.2.1 - 2026-11-19

- Serialize `datetime.date`, `datetime.datetime`, `datetime.time`, `datetime.timedelta`, `Decimal`, `UUID`, and `Path`/`PurePath` cleanly in `take_snapshot()` instead of falling back to type placeholders (and mark them immutable in `is_fast_immutable()`), while preferring `model_dump(mode="json")` with fallback to `model_dump()`.
- Expose `max_depth: int = 3` and `max_items: int = 20` on `Trail(...)` and forward them through `input_summary`, `_snap()`, and `branch()`.

## 0.2.0 - 2026-11-19

- Added ambient `ContextVar` trail activation (`with trail.activate():`, `current_trail()`, module-level `papertrail.set` / `record_change` / `decide` / `note` no-ops when unset) for deep helpers and `ThreadPoolExecutor` worker closures.
- Added external `watchers={"name": callable}` support across `Trail(...)` and `trail.stage(...)` so in-place mutations on external stores (such as session plans) are diffed alongside the main state.
- Added key-level `dict_delta` for `Mapping` stage diffs and compact key-by-key Markdown rendering.
- Added `Trail(enabled=False)` / `NullTrail`, `watch_default=(...)` field allowlist, and `is_fast_immutable` `old is new` fast-skip in `stage()`.
- Added `trail.stage_timings() -> dict[str, float]` returning ordered stage wall-clock timings.

## 0.1.0 - 2026-11-19

- Initial release of `papertrail`: zero-dependency `Trail` state/decision provenance ledger, bounded/redacted snapshotting (`snapshot.py`), incremental `stage(..., watch=...)` diffing, thread/async `branch()` + `merge()`, Markdown/JSONL export, and `papertrail show` CLI.
