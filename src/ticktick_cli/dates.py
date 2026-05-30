"""Flexible date input parser for the `edit` / `punt` subcommands.

Inputs (case-insensitive for named forms):
- ISO 8601 with offset: `2026-06-15T09:00:00+0000` — pass-through.
- Relative duration: `+7d`, `7d`, `+1w`, `3h`, `90m` — added to now.
- Named day-of-week: `monday`..`sunday` — next occurrence at 00:00.
- `next-monday`..`next-sunday` — alias for the bare day name.
- `today`, `tomorrow` — 00:00 of the named day.

Output is always TickTick's expected ISO 8601 shape:
`YYYY-MM-DDTHH:MM:SS±HHMM`.

A `now` arg is exposed for testing; in production callers pass nothing
and we default to `datetime.now(timezone.utc).astimezone()` (local tz)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_DURATION_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _format(dt: datetime) -> str:
    """TickTick uses `YYYY-MM-DDTHH:MM:SS±HHMM` (no colon in offset)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


_ISO_8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{4}|Z)$"
)


def _is_iso_8601(s: str) -> bool:
    """Match TickTick's expected shape: `YYYY-MM-DDTHH:MM:SS±HHMM` or
    `...Z`. Stricter than full ISO 8601 (e.g. `+HH:MM` offset rejected)
    on purpose — pass-through means we never re-normalize, so the form
    we accept here is the form we'll forward to TickTick verbatim."""
    return bool(_ISO_8601_RE.match(s))


def parse_when(spec: str, *, now: datetime | None = None) -> str:
    """Parse a flexible date spec and return TickTick-formatted ISO 8601.

    See module docstring for accepted forms. Raises ValueError on
    unparseable input (including empty string)."""
    if not spec:
        raise ValueError("Cannot parse empty date spec")
    if now is None:
        now = datetime.now(timezone.utc).astimezone()

    raw = spec.strip()
    if _is_iso_8601(raw):
        return raw

    lowered = raw.lower()

    if lowered == "today":
        return _format(now.replace(hour=0, minute=0, second=0, microsecond=0))
    if lowered == "tomorrow":
        target = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return _format(target)

    # next-monday → monday
    if lowered.startswith("next-"):
        lowered = lowered[5:]
    if lowered in _WEEKDAYS:
        target_wd = _WEEKDAYS[lowered]
        # `now.weekday()`: Monday=0..Sunday=6. Always step forward at
        # least one day so "punt to today's weekday" jumps to next week
        # rather than collapsing to today.
        delta = (target_wd - now.weekday()) % 7
        if delta == 0:
            delta = 7
        target = (now + timedelta(days=delta)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return _format(target)

    # Relative duration: `+7d`, `7d`, `+1w`, `3h`, `90m`.
    body = lowered[1:] if lowered.startswith("+") else lowered
    if body and body[-1] in _DURATION_UNITS:
        try:
            n = int(body[:-1])
        except ValueError as e:
            raise ValueError(f"Cannot parse date spec {spec!r}") from e
        kwargs = {_DURATION_UNITS[body[-1]]: n}
        return _format(now + timedelta(**kwargs))

    raise ValueError(
        f"Cannot parse date spec {spec!r}. Expected ISO 8601, a "
        f"relative duration like '+7d', a weekday name like 'monday', "
        f"or 'today'/'tomorrow'."
    )
