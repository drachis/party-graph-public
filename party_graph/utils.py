"""Small shared utilities."""
from __future__ import annotations

from datetime import time as dtime
from datetime import datetime


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


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
