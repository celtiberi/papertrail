"""Zero-dependency per-turn state and decision provenance ledger."""

from .formatters import format_markdown
from .trail import (
    Entry,
    NullTrail,
    StageSummary,
    Trail,
    current_trail,
    decide,
    note,
    record_change,
    set,
)

__version__ = "0.2.0"

__all__ = [
    "Entry",
    "NullTrail",
    "StageSummary",
    "Trail",
    "__version__",
    "current_trail",
    "decide",
    "format_markdown",
    "note",
    "record_change",
    "set",
]
