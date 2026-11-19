"""CLI viewer for papertrail JSONL and JSON logs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .formatters import format_markdown


def _load_trails(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    if raw.startswith("{") and "\n" not in raw:
        return [json.loads(raw)]
    # Handle pretty-printed single JSON file vs multi-line JSONL cleanly.
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "entries" in parsed:
                return [parsed]
        except json.JSONDecodeError:
            pass
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        clean = line.strip()
        if clean:
            records.append(json.loads(clean))
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="papertrail", description="Inspect papertrail turn logs.")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="Render a recorded trail from a JSON or JSONL file.")
    show.add_argument("path", help="Path to JSONL or JSON trail file.")
    show.add_argument("--name", default=None, help="Filter records by trail name before selecting --index.")
    show.add_argument("--index", type=int, default=-1, help="0-based or negative record index (default: -1, latest).")
    show.add_argument("--field", default=None, help="Only show entries touching a specific field/decision name.")
    show.add_argument("--decisions", action="store_true", help="Only show decisions and errors.")
    show.add_argument(
        "--all-stages",
        action="store_true",
        help="Include idle stages (0 changes/decisions/entries, ok, < 1 ms) in Markdown output.",
    )
    show.add_argument("--json", dest="as_json", action="store_true", help="Print matching record as JSON.")

    args = parser.parse_args(argv)
    if args.command != "show":
        return 2

    target = Path(args.path).expanduser()
    if not target.exists():
        print(f"File not found: {target}", file=sys.stderr)
        return 1

    trails = _load_trails(target)
    if args.name is not None:
        trails = [t for t in trails if t.get("name") == args.name]
    if not trails:
        print("No matching trails found.", file=sys.stderr)
        return 1

    try:
        chosen = dict(trails[args.index])
    except IndexError:
        print(f"Index {args.index} out of range for {len(trails)} trail(s).", file=sys.stderr)
        return 1

    if args.as_json:
        if args.field is not None or args.decisions:
            chosen["entries"] = [
                e
                for e in chosen.get("entries", [])
                if (args.field is None or e.get("field") == args.field)
                and (not args.decisions or e.get("kind") in ("decision", "error"))
            ]
        print(json.dumps(chosen, ensure_ascii=False, indent=2))
        return 0

    print(
        format_markdown(
            chosen,
            include_evidence=True,
            field=args.field,
            decisions_only=args.decisions,
            all_stages=args.all_stages,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
