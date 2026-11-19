"""Human-readable Markdown formatting for Trail exports and JSONL debug artifacts."""

from __future__ import annotations

import json
from typing import Any


def _short_repr(value: Any, max_len: int = 120) -> str:
    if isinstance(value, str):
        rendered = repr(value)
    else:
        try:
            rendered = json.dumps(value, ensure_ascii=False)
        except Exception:
            rendered = repr(value)
    if len(rendered) <= max_len:
        return rendered
    return rendered[: max_len - 3] + "..."


def _flatten_dict_diff(before: dict[str, Any], after: dict[str, Any], prefix: str = "") -> list[str]:
    parts: list[str] = []
    for key in list(dict.fromkeys((*before.keys(), *after.keys()))):
        full_key = f"{prefix}{key}"
        b_has = key in before
        a_has = key in after
        b_val = before.get(key)
        a_val = after.get(key)
        if b_has and a_has and isinstance(b_val, dict) and isinstance(a_val, dict):
            parts.extend(_flatten_dict_diff(b_val, a_val, f"{full_key}."))
            continue
        b_txt = _short_repr(b_val) if b_has else "<unset>"
        a_txt = _short_repr(a_val) if a_has else "<unset>"
        parts.append(f"{full_key}: {b_txt} -> {a_txt}")
    return parts


def _is_idle_stage(st: dict[str, Any], active_stage_keys: set[tuple[Any, Any]]) -> bool:
    return (
        st.get("status", "ok") == "ok"
        and int(st.get("changes") or 0) == 0
        and int(st.get("decisions") or 0) == 0
        and float(st.get("duration_ms") or 0.0) < 1.0
        and (st.get("stage"), st.get("branch")) not in active_stage_keys
    )


def format_markdown(
    data: dict[str, Any],
    *,
    include_evidence: bool = True,
    field: str | None = None,
    decisions_only: bool = False,
    all_stages: bool = False,
) -> str:
    """Format a Trail dictionary as a readable Markdown timeline."""
    name = str(data.get("name") or "run")
    total_ms = float(data.get("total_ms") or 0.0)
    raw_entries = list(data.get("entries") or [])
    stages = list(data.get("stages") or [])

    filtered: list[dict[str, Any]] = []
    for entry in raw_entries:
        if field is not None and entry.get("field") != field:
            continue
        if decisions_only and entry.get("kind") not in ("decision", "error"):
            continue
        filtered.append(entry)

    lines: list[str] = [f"### Papertrail: {name} ({len(filtered)} entries, {total_ms:.1f} ms)"]
    input_summary = data.get("input_summary")
    if input_summary is not None:
        lines.append(f"**Input:** `{_short_repr(input_summary, max_len=200)}`")

    if not filtered:
        lines.append("")
        lines.append("_No matching entries recorded._")
    else:
        lines.append("")
        for entry in filtered:
            seq = entry.get("seq", "?")
            stage = entry.get("stage") or "root"
            branch = entry.get("branch")
            loc = f"{stage} | {branch}" if branch else stage
            at_ms = float(entry.get("at_ms") or 0.0)
            caller = entry.get("caller") or ""
            caller_part = f" `{caller}`" if caller else ""
            kind = entry.get("kind") or "note"
            f_name = entry.get("field")

            if kind in ("set", "change", "diff"):
                b_val = entry.get("before")
                a_val = entry.get("after")
                if kind == "diff" and isinstance(b_val, dict) and isinstance(a_val, dict):
                    delta_parts = _flatten_dict_diff(b_val, a_val)
                    delta_text = ", ".join(delta_parts) if delta_parts else "{}"
                    headline = f"**diff** `{f_name}`: {delta_text}"
                else:
                    headline = (
                        f"**{kind}** `{f_name}`: `{_short_repr(b_val)}` -> "
                        f"`{_short_repr(a_val)}`"
                    )
            elif kind == "decision":
                headline = f"**decision** `{f_name}` = `{_short_repr(entry.get('outcome'))}`"
            elif kind == "error":
                headline = f"**error** `{f_name}`: `{_short_repr(entry.get('reason'))}`"
            else:
                headline = f"**note**: {entry.get('reason') or ''}"

            lines.append(f"{seq}. `[{loc}]` (+{at_ms:.1f} ms){caller_part} — {headline}")
            if kind != "note" and entry.get("reason"):
                lines.append(f"   why: {entry['reason']}")
            if include_evidence and entry.get("evidence"):
                lines.append(f"   evidence: `{json.dumps(entry['evidence'], ensure_ascii=False)}`")

    if stages and field is None and not decisions_only:
        if all_stages:
            visible_stages = stages
            omitted = 0
        else:
            active_stage_keys = {(e.get("stage"), e.get("branch")) for e in raw_entries}
            visible_stages = [st for st in stages if not _is_idle_stage(st, active_stage_keys)]
            omitted = len(stages) - len(visible_stages)

        if visible_stages:
            lines.append("")
            lines.append("| Stage | Branch | Status | Changes | Decisions | Duration |")
            lines.append("|---|---|---|---|---|---|")
            for st in visible_stages:
                lines.append(
                    f"| `{st.get('stage', '')}` | `{st.get('branch') or '-'}` | "
                    f"{st.get('status', 'ok')} | {st.get('changes', 0)} | "
                    f"{st.get('decisions', 0)} | {float(st.get('duration_ms') or 0.0):.1f} ms |"
                )
        if omitted > 0:
            if not visible_stages:
                lines.append("")
            lines.append(
                f"_({omitted} idle {'stage' if omitted == 1 else 'stages'} "
                "< 1 ms omitted; pass --all-stages to show all)_"
            )

    return "\n".join(lines)
