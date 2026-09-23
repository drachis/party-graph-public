"""Small shared utilities."""
from __future__ import annotations

import threading
from datetime import date as ddate
from datetime import time as dtime
from datetime import datetime, timezone
from typing import Callable, TypeVar
from zoneinfo import ZoneInfo

T = TypeVar("T")

# Matches browser.py's configured timezone_id -- both the RSVP/host CSVs'
# UTC timestamps and Partiful's own displayed event dates should agree
# once converted to the same local zone.
LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def run_with_timeout(fn: Callable[[], T], timeout: float,
                      on_timeout: Callable[[], None] | None = None) -> T:
    """Run fn() with a hard wall-clock ceiling, for calls that can hang
    with no timeout of their own.

    DO NOT wrap Playwright sync-API calls with this. Tried exactly that
    for a frozen-tab watchdog and it broke every call instantly with
    "Cannot switch to a different thread": Playwright's sync API runs on
    a greenlet tied to the thread that created it and cannot be driven
    from a second thread at all, timeout or not. Reverted; see
    scraper.StuckNavigation for the safe (same-thread) fix for that
    specific symptom (a tab silently landing on about:blank). A real fix
    for a fully wedged browser/CDP connection would need a subprocess
    (killable at the OS level) rather than a thread -- not implemented.

    Fine for genuinely thread-safe/blocking work in general: fn() runs in
    a daemon worker thread; if it hasn't returned within `timeout`
    seconds, on_timeout() is called (expected to make it stop blocking --
    what that takes depends entirely on what fn() does) and we wait a
    short grace period for it to unwind. Raises TimeoutError if it's still
    stuck after that grace period; the worker thread is then abandoned
    (daemon=True keeps it from blocking process exit).
    """
    outcome: dict = {}

    def worker() -> None:
        try:
            outcome["value"] = fn()
        except Exception as e:  # re-raised on the caller's thread below
            outcome["error"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive() and on_timeout is not None:
        try:
            on_timeout()
        except Exception:
            pass
        t.join(15)
    if t.is_alive():
        raise TimeoutError(f"operation did not return within {timeout:.0f}s (+15s grace)")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")  # type: ignore[return-value]


def looks_like_closed_browser(e: Exception) -> bool:
    """True if `e` means the browser/context/page died out from under us
    (Playwright's TargetClosedError) rather than a one-off page failure.

    Not imported directly: TargetClosedError only lives in Playwright's
    private `_impl._errors` module, not the public `sync_api`. Detecting
    it by class name/message is more stable across Playwright versions
    than reaching into that private module, and this is the difference
    between "skip this one item and retry/continue" and "the browser is
    gone, stop the whole run now" -- retrying or moving to the next item
    just fails the same way instantly, for every remaining item.
    """
    return "TargetClosedError" in type(e).__name__ or "has been closed" in str(e)


def parse_rsvp_timestamp(raw: object) -> datetime | None:
    """Parse rsvp_details.csv's 'rsvp_date', e.g. 'Sep 08, 2023 03:51:32 PM UTC'.

    Shared by the gather-dates scraper (to sort events reverse-chronologically)
    and event_calendar.py (to place RSVPs on the calendar).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith(" UTC"):
        s = s[: -len(" UTC")]
    try:
        return datetime.strptime(s, "%b %d, %Y %I:%M:%S %p")
    except ValueError:
        return None


def parse_rsvp_local_date(raw: object) -> ddate | None:
    """Like parse_rsvp_timestamp, but returns the LOCAL (America/Los_Angeles)
    calendar date -- for placing something on a specific day, not just
    sorting. The CSV timestamps are UTC; a Pacific evening RSVP or event
    routinely falls on the *next* UTC day (e.g. "Dec 30, 2025 12:30 AM
    UTC" is actually 4:30 PM Dec 29 in Los Angeles). Taking .date() on the
    naive UTC value directly -- as if it barely mattered -- silently
    misplaces roughly 40% of real rows by a full day. Use this, not
    parse_rsvp_timestamp(...).date(), anywhere the result lands on a
    calendar cell.
    """
    dt = parse_rsvp_timestamp(raw)
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).date()
