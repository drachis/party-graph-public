"""Small shared utilities."""
from __future__ import annotations

import threading
from datetime import time as dtime
from datetime import datetime
from typing import Callable, TypeVar

T = TypeVar("T")


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
