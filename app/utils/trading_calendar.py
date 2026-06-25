"""NYSE trading-calendar utility (PIPELINE/73_Trading_Calendar_Spec.md).

Single source of truth for US (NYSE) trading-session date arithmetic across the
platform. Per the trading-calendar spec it is backed by
``pandas_market_calendars`` with the ``NYSE`` calendar; all business-day
calculations use real NYSE sessions (weekends *and* exchange holidays excluded),
never naive weekday math.

Public functions
----------------
- :func:`is_trading_day`
- :func:`previous_trading_day`
- :func:`next_trading_day`
- :func:`add_trading_days`
- :func:`trading_days_between`

Design notes
------------
``pandas_market_calendars`` is a declared project runtime dependency
(``pyproject.toml`` / ``requirements.txt``). It is imported lazily inside the
calendar accessor so that modules which only ever inject a fake calendar in
tests (e.g. the Module 16 outcome queue) do not pull the dependency at import
time. The NYSE calendar object is cached after first construction.

This module performs no database, provider, or trading logic. It only answers
calendar questions about NYSE sessions.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Final

# Name of the market-calendars calendar to use for every query.
CALENDAR_NAME: Final[str] = "NYSE"

# Cached calendar instance (built lazily on first use).
_CALENDAR: Any | None = None


def _calendar() -> Any:
    """Return the cached NYSE calendar, building it on first use.

    The :mod:`pandas_market_calendars` import is deliberately lazy so importing
    this module never fails in environments where the dependency is absent but
    unused (callers that inject a fake calendar in tests).
    """
    global _CALENDAR
    if _CALENDAR is None:
        import pandas_market_calendars as mcal  # lazy import

        _CALENDAR = mcal.get_calendar(CALENDAR_NAME)
    return _CALENDAR


def _sessions(start: date, end: date) -> list[date]:
    """Return all NYSE session dates in the inclusive ``[start, end]`` range.

    Returns an empty list when ``start > end``. Session timestamps from
    ``valid_days`` are normalised to plain :class:`datetime.date` objects.
    """
    if start > end:
        return []
    valid = _calendar().valid_days(
        start_date=start.isoformat(), end_date=end.isoformat()
    )
    # ``valid_days`` returns a (tz-aware) DatetimeIndex; take the calendar date.
    return [ts.date() for ts in valid]


def is_trading_day(day: date) -> bool:
    """Return ``True`` if ``day`` is an NYSE trading session."""
    return bool(_sessions(day, day))


def next_trading_day(day: date) -> date:
    """Return the first NYSE session strictly after ``day``.

    The window is expanded until at least one session is found so long holiday
    stretches are handled correctly.
    """
    span = 7
    while True:
        sessions = _sessions(day + timedelta(days=1), day + timedelta(days=span))
        if sessions:
            return sessions[0]
        span *= 2


def previous_trading_day(day: date) -> date:
    """Return the last NYSE session strictly before ``day``."""
    span = 7
    while True:
        sessions = _sessions(day - timedelta(days=span), day - timedelta(days=1))
        if sessions:
            return sessions[-1]
        span *= 2


def add_trading_days(day: date, n: int) -> date:
    """Return the NYSE session ``n`` sessions away from ``day``.

    ``day`` is treated as session index ``0`` (it must itself be a trading
    session for the index to be exact; the Module 16 caller always passes an
    ``entry_date`` that is a session). ``n`` may be positive (forward) or
    negative (backward); ``n == 0`` returns ``day`` unchanged when it is a
    session. The lookahead window grows until enough sessions are collected.

    Raises
    ------
    ValueError
        If ``day`` is not an NYSE trading session (so the session index is
        undefined).
    """
    if not is_trading_day(day):
        raise ValueError(f"{day.isoformat()} is not an NYSE trading session")
    if n == 0:
        return day

    # ~1.6 calendar days per session plus a generous holiday cushion.
    span = abs(n) * 2 + 14
    while True:
        if n > 0:
            sessions = _sessions(day, day + timedelta(days=span))
            if len(sessions) > n:
                return sessions[n]
        else:
            sessions = _sessions(day - timedelta(days=span), day)
            if len(sessions) > abs(n):
                return sessions[len(sessions) - 1 + n]
        span *= 2


def trading_days_between(start: date, end: date) -> list[date]:
    """Return every NYSE session in the inclusive ``[start, end]`` range."""
    return _sessions(start, end)


__all__ = [
    "CALENDAR_NAME",
    "is_trading_day",
    "previous_trading_day",
    "next_trading_day",
    "add_trading_days",
    "trading_days_between",
]
