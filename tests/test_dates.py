"""Tests for the flexible date parser used by `edit` / `punt`.

The parser accepts ISO 8601 (passed through verbatim), relative
durations (`+7d`, `3h`), named days (`monday`...`sunday` and the
`next-` aliases), and the literals `today` / `tomorrow`. Output is
always TickTick's ISO 8601 shape: `YYYY-MM-DDTHH:MM:SS±HHMM`.

All tests inject a fixed `now` so we don't depend on wall-clock
behavior."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ticktick_cli.dates import parse_when


# ---- Helpers ----------------------------------------------------------------


def _now(*, year=2026, month=5, day=30, hour=14, minute=23, second=0,
         tz=timezone.utc) -> datetime:
    """A Saturday in late May 2026, 14:23 UTC."""
    return datetime(year, month, day, hour, minute, second, tzinfo=tz)


# ---- ISO 8601 pass-through --------------------------------------------------


def test_parse_when_iso_8601_passes_through():
    """Anything that already looks like ISO 8601 is returned verbatim —
    we don't re-normalize the offset or seconds field."""
    iso = "2026-06-15T09:00:00+0000"
    assert parse_when(iso, now=_now()) == iso


def test_parse_when_iso_8601_with_nonzero_offset_preserved():
    iso = "2026-06-15T15:00:00+0500"
    assert parse_when(iso, now=_now()) == iso


def test_parse_when_rejects_partial_iso_8601():
    """`2026-06-15T09:00` (no seconds, no offset) used to pass through
    silently and 400 at TickTick. Now the parser refuses it before the
    HTTP layer ever sees it. Users wanting precision should pass the
    full `YYYY-MM-DDTHH:MM:SS±HHMM` form."""
    with pytest.raises(ValueError, match="Cannot parse"):
        parse_when("2026-06-15T09:00", now=_now())


def test_parse_when_accepts_iso_8601_with_z_suffix():
    """`Z` is the ISO 8601 alias for `+0000`; pass through verbatim."""
    iso = "2026-06-15T09:00:00Z"
    assert parse_when(iso, now=_now()) == iso


# ---- Relative durations -----------------------------------------------------


def test_parse_when_plus_7d_advances_by_seven_days():
    result = parse_when("+7d", now=_now())
    assert result == "2026-06-06T14:23:00+0000"


def test_parse_when_bare_7d_works_too():
    """The `+` is optional — `7d` and `+7d` are equivalent."""
    assert parse_when("7d", now=_now()) == "2026-06-06T14:23:00+0000"


def test_parse_when_plus_1w_advances_by_one_week():
    assert parse_when("+1w", now=_now()) == "2026-06-06T14:23:00+0000"


def test_parse_when_plus_3h_advances_by_three_hours():
    assert parse_when("+3h", now=_now()) == "2026-05-30T17:23:00+0000"


def test_parse_when_plus_90m_advances_by_ninety_minutes():
    assert parse_when("+90m", now=_now()) == "2026-05-30T15:53:00+0000"


# ---- Named days -------------------------------------------------------------


def test_parse_when_monday_resolves_to_next_monday_midnight_local():
    """now=Saturday 2026-05-30. Next Monday is 2026-06-01."""
    # Use UTC for the test so "local midnight" == UTC midnight.
    result = parse_when("monday", now=_now())
    assert result == "2026-06-01T00:00:00+0000"


def test_parse_when_next_monday_is_alias_for_monday():
    assert parse_when("next-monday", now=_now()) == parse_when("monday", now=_now())


def test_parse_when_sunday_when_today_is_saturday():
    """Saturday → next Sunday is tomorrow."""
    result = parse_when("sunday", now=_now())
    assert result == "2026-05-31T00:00:00+0000"


def test_parse_when_named_day_when_today_is_that_day_jumps_to_next_week():
    """Today is Saturday. Asking for 'saturday' goes 7 days forward —
    NOT today. Otherwise 'punt to saturday' on a Saturday would mean
    'punt to today', which contradicts the verb."""
    result = parse_when("saturday", now=_now())
    assert result == "2026-06-06T00:00:00+0000"


# ---- today / tomorrow -------------------------------------------------------


def test_parse_when_today_resolves_to_midnight_local_today():
    assert parse_when("today", now=_now()) == "2026-05-30T00:00:00+0000"


def test_parse_when_tomorrow_resolves_to_midnight_local_tomorrow():
    assert parse_when("tomorrow", now=_now()) == "2026-05-31T00:00:00+0000"


# ---- Errors -----------------------------------------------------------------


def test_parse_when_rejects_garbage():
    with pytest.raises(ValueError, match="Cannot parse"):
        parse_when("flarble", now=_now())


def test_parse_when_rejects_next_with_unknown_day():
    """`next-<word>` strips the prefix and falls through; if the
    remainder isn't a weekday, the standard 'Cannot parse' error
    fires. Pinned so a future 'treat next- as a generic prefix' change
    doesn't silently swallow garbage."""
    with pytest.raises(ValueError, match="Cannot parse"):
        parse_when("next-flarble", now=_now())


def test_parse_when_rejects_empty_string():
    """Empty string is reserved as the 'clear this date' sentinel at
    the API layer; the parser refuses to translate it. CLI handlers
    map `--clear-due` directly to `""` without going through here."""
    with pytest.raises(ValueError):
        parse_when("", now=_now())


def test_parse_when_case_insensitive_for_named_inputs():
    """Forgiving: `MONDAY` and `Tomorrow` work the same as lowercase."""
    assert parse_when("MONDAY", now=_now()) == parse_when("monday", now=_now())
    assert parse_when("Tomorrow", now=_now()) == parse_when("tomorrow", now=_now())
