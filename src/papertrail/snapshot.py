"""Bounded, JSON-safe snapshotting and state field access helpers."""

from __future__ import annotations

import collections
import dataclasses
import itertools
import math
from collections.abc import Callable, Iterable, Mapping, Sequence, Sized
from collections.abc import Set as AbstractSet
from enum import Enum
from typing import Any

REDACTED = "[REDACTED]"

DEFAULT_MASK_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "api_key",
        "apikey",
        "x_api_key",
        "access_token",
        "refresh_token",
        "secret",
        "secret_key",
        "private_key",
        "client_secret",
        "auth_token",
        "token",
    }
)

_MISSING = object()


def _normalize_key(key: str) -> str:
    return key.strip().lower().replace("-", "_")


def _truncate_str(text: str, max_str: int, redact: Callable[[str], str] | None) -> str:
    cleaned = redact(text) if redact is not None else text
    limit = max(0, max_str)
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}...[+{len(cleaned) - limit} chars]"


def _is_plain_object_with_attrs(value: Any) -> bool:
    if callable(value) or isinstance(value, type):
        return False
    mod = getattr(type(value), "__module__", "")
    # Skip stdlib locks, sockets, IO handles, and internal runtime handles.
    if mod.startswith(("_thread", "threading", "io", "_io", "socket", "asyncio", "concurrent")):
        return False
    return hasattr(value, "__dict__") and bool(vars(value))


def take_snapshot(
    value: Any,
    *,
    max_depth: int = 3,
    max_items: int = 20,
    max_str: int = 300,
    redact: Callable[[str], str] | None = None,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> Any:
    """Convert any Python value into a bounded, JSON-serializable snapshot."""
    if isinstance(value, Enum):
        value = value.value

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _truncate_str(value, max_str, redact)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value[: max(0, max_str) + 64]).decode("utf-8", errors="replace")
        total = len(value)
        if len(raw) > max_str or total > len(raw):
            clipped = redact(raw[: max(0, max_str)]) if redact else raw[: max(0, max_str)]
            return f"{clipped}...[+{total} bytes]"
        return redact(raw) if redact else raw

    if _depth >= max_depth:
        if isinstance(value, Mapping):
            return f"<dict len={len(value)} max_depth>"
        if isinstance(value, (list, tuple, set, frozenset, collections.deque)):
            return f"<{type(value).__name__} len={len(value)} max_depth>"
        return f"<{type(value).__qualname__} max_depth>"

    seen = _seen if _seen is not None else set()
    obj_id = id(value)
    if obj_id in seen:
        return "<circular>"

    seen.add(obj_id)
    try:
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for idx, (k, v) in enumerate(value.items()):
                if idx >= max_items:
                    out["__truncated__"] = max(0, len(value) - max_items)
                    break
                key_str = _truncate_str(str(k), max_str, redact)
                if _normalize_key(key_str) in DEFAULT_MASK_KEYS and isinstance(v, str) and v.strip():
                    out[key_str] = REDACTED
                else:
                    out[key_str] = take_snapshot(
                        v,
                        max_depth=max_depth,
                        max_items=max_items,
                        max_str=max_str,
                        redact=redact,
                        _depth=_depth + 1,
                        _seen=seen,
                    )
            return out

        nt_fields = getattr(value, "_fields", None)
        if isinstance(value, tuple) and isinstance(nt_fields, tuple):
            mapped = {str(f): getattr(value, str(f), None) for f in nt_fields}
            return take_snapshot(
                mapped,
                max_depth=max_depth,
                max_items=max_items,
                max_str=max_str,
                redact=redact,
                _depth=_depth,
                _seen=seen,
            )

        if (
            isinstance(value, (list, tuple, set, frozenset, collections.deque, Sequence, AbstractSet))
            and not isinstance(value, (str, bytes, bytearray, memoryview))
        ):
            seq: Iterable[Any]
            if isinstance(value, (set, frozenset)):
                try:
                    seq = sorted(value, key=lambda x: repr(x))
                except Exception:
                    seq = value
            else:
                seq = value

            items: list[Any] = []
            for idx, item in enumerate(itertools.islice(seq, max_items + 1)):
                if idx >= max_items:
                    if isinstance(value, Sized):
                        items.append(f"...[+{max(0, len(value) - max_items)} more]")
                    else:
                        items.append("...[more]")
                    break
                items.append(
                    take_snapshot(
                        item,
                        max_depth=max_depth,
                        max_items=max_items,
                        max_str=max_str,
                        redact=redact,
                        _depth=_depth + 1,
                        _seen=seen,
                    )
                )
            return items

        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            dc_map: dict[str, Any] = {}
            for f in dataclasses.fields(value):
                attr = getattr(value, f.name, _MISSING)
                if attr is not _MISSING:
                    dc_map[f.name] = attr
            return take_snapshot(
                dc_map,
                max_depth=max_depth,
                max_items=max_items,
                max_str=max_str,
                redact=redact,
                _depth=_depth,
                _seen=seen,
            )

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return take_snapshot(
                    model_dump(),
                    max_depth=max_depth,
                    max_items=max_items,
                    max_str=max_str,
                    redact=redact,
                    _depth=_depth,
                    _seen=seen,
                )
            except Exception:
                pass

        if _is_plain_object_with_attrs(value):
            public_attrs = {k: v for k, v in vars(value).items() if not str(k).startswith("_")}
            if public_attrs:
                return take_snapshot(
                    public_attrs,
                    max_depth=max_depth,
                    max_items=max_items,
                    max_str=max_str,
                    redact=redact,
                    _depth=_depth,
                    _seen=seen,
                )

        return f"<{type(value).__qualname__}>"
    finally:
        seen.discard(obj_id)


def read_field(state: Any, field: str) -> Any:
    """Read a field from a dict or object without raising on missing entries."""
    if state is None:
        return None
    if isinstance(state, Mapping):
        return state.get(field, None)
    return getattr(state, field, None)


def write_field(state: Any, field: str, value: Any) -> None:
    """Write a field onto a mutable dict or object."""
    if state is None:
        raise ValueError("Cannot use Trail.set() when Trail was initialized with state=None; use record_change().")
    if isinstance(state, dict):
        state[field] = value
        return
    setattr(state, field, value)


def is_fast_immutable(value: Any) -> bool:
    """Return True if identity comparison (old is new) safely proves value did not change."""
    if value is None or isinstance(value, (bool, int, float, str, bytes, Enum)):
        return True
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        params = getattr(type(value), "__dataclass_params__", None)
        return bool(params and getattr(params, "frozen", False))
    if isinstance(value, tuple) and len(value) <= 32:
        return all(is_fast_immutable(item) for item in value)
    return False


def dict_delta(before: dict[str, Any], after: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract only added, removed, or modified keys between two snapshot dicts."""
    before_out: dict[str, Any] = {}
    after_out: dict[str, Any] = {}
    for key in list(dict.fromkeys((*before.keys(), *after.keys()))):
        in_b = key in before
        in_a = key in after
        if in_b and not in_a:
            before_out[key] = before[key]
        elif in_a and not in_b:
            after_out[key] = after[key]
        else:
            b_val = before[key]
            a_val = after[key]
            if b_val == a_val:
                continue
            if isinstance(b_val, dict) and isinstance(a_val, dict):
                sub_b, sub_a = dict_delta(b_val, a_val)
                if sub_b or sub_a:
                    before_out[key] = sub_b
                    after_out[key] = sub_a
            else:
                before_out[key] = b_val
                after_out[key] = a_val
    return before_out, after_out


def list_watch_fields(
    state: Any,
    watch: str | Sequence[str] | None,
    exclude: Iterable[str] = (),
) -> list[str]:
    """Resolve field names to snapshot around a stage boundary."""
    if state is None or watch is None:
        return []
    skip = set(exclude)
    if watch == "*":
        if isinstance(state, Mapping):
            return [str(k) for k in state.keys() if not str(k).startswith("_") and str(k) not in skip]
        if dataclasses.is_dataclass(state) and not isinstance(state, type):
            return [f.name for f in dataclasses.fields(state) if not f.name.startswith("_") and f.name not in skip]
        if hasattr(state, "__dict__"):
            return [str(k) for k in vars(state).keys() if not str(k).startswith("_") and str(k) not in skip]
        return []
    if isinstance(watch, str):
        return [watch] if watch not in skip else []
    return [str(f) for f in watch if str(f) not in skip]
