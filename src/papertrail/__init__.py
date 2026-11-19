"""Zero-dependency per-turn state and decision provenance ledger."""

from .formatters import format_markdown
from .trail import Entry, StageSummary, Trail

__version__ = "0.1.0"

__all__ = [
    "Entry",
    "StageSummary",
    "Trail",
    "__version__",
    "format_markdown",
]
