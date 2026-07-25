"""Primitives shared by the monitor cycle and every provider.

These live outside monitor.py so a provider module can use them without
importing the cycle that imports it (monitor -> providers -> common).
"""

from __future__ import annotations

from datetime import date

# The bound on every single line that may reach run_summaries.errors, whoever
# composed it — one of the cycle's own renderings or a provider's poll failure.
# run_summaries is world-readable (see monitor.py "two rendering channels"), so
# this is part of the published error contract rather than a provider detail.
MAX_ERROR_MESSAGE_CHARS = 200


def as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def date_in_watch(d: date, start: date, end: date) -> bool:
    """Nights of the stay: check-out day availability is irrelevant."""
    if end > start:
        return start <= d < end
    return d == start


def capped_line(text: str) -> str:
    """One line, no longer than MAX_ERROR_MESSAGE_CHARS: the bound every message
    that reaches run_summaries.errors must respect, whatever built it."""
    text = " ".join(text.split())
    if len(text) > MAX_ERROR_MESSAGE_CHARS:
        text = text[: MAX_ERROR_MESSAGE_CHARS - 1] + "…"
    return text
