"""Central config: paths, pacing, user agents, regexes, anti-detection script."""
from __future__ import annotations

import re
from datetime import time as dtime
from pathlib import Path

# --- Paths ---
CSV_IN = Path("data-request/rsvp_details.csv")
HOST_CSV_IN = Path("data-request/host_details.csv")  # events you host, not RSVP'd
OUT_DIR = Path("scraped_events")
SUMMARY = Path("scraped_events/summary.csv")
STATE = Path("scraped_events/scrape_state.json")
USER_DATA_DIR = Path("scraped_events/.browser")
# Lightweight date-only cache written by --gather-dates: {token: {...}}.
# Separate from OUT_DIR's per-event JSON, which carries the full guest list.
EVENT_DATES = Path("scraped_events/event_dates.json")

# --- Gather-dates pacing (date-only pass; no guest-list interaction) ---
# Deliberately slow -- reads like someone actually browsing, not a script.
GATHER_DWELL_MIN = 20.0
GATHER_DWELL_MAX = 30.0
GATHER_GAP_MIN = 12.0
GATHER_GAP_MAX = 28.0
GATHER_LONG_PAUSE_CHANCE = 0.15  # occasional longer break, like a distracted human
GATHER_LONG_PAUSE_MIN = 40.0
GATHER_LONG_PAUSE_MAX = 100.0
# Stop the run if this many pages in a row come back dateless/failed --
# more likely a soft block than that many unrelated individual glitches.
GATHER_EMPTY_STREAK_LIMIT = 3

# --- Pacing ---
MIN_DAILY = 8
MAX_DAILY = 12
SPREAD_HOURS = 1.0
WINDOW_START = "19:00"
WINDOW_END = "23:00"
HEADLESS = False

# --- User agents ---
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

# --- Regexes ---
TOKEN_RE = re.compile(r"partiful\.com/e/([A-Za-z0-9_-]+)")
RSPV_STATS_RE = re.compile(
    r"(\d+)\s*Going\s*\u00b7\s*(\d+)\s*Interested\s*\u00b7\s*(\d+)\s*Maybe"
)
# Partiful appends ", YYYY" when the date isn't in the current year window
# (e.g. past events from prior years) -- year suffix is optional here.
DATE_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}(,\s+\d{4})?$"
)
# Single time ("8:00pm") or a start-end range ("4:30pm – 9:00pm").
TIME_RE = re.compile(
    r"^\d{1,2}(:\d{2})?\s*(AM|PM|am|pm)"
    r"(\s*[–-]\s*\d{1,2}(:\d{2})?\s*(AM|PM|am|pm))?\s*$"
)

# Multi-day events render their date as two lines instead of one, with an
# abbreviated weekday and the time inline (middot-separated), e.g.:
#   "Fri, Sep 11 · 7:00pm —"
#   "Sun, Sep 13 · 10:00pm"
# Distinct enough from DATE_RE/TIME_RE (full weekday name, date and time on
# separate lines) that it needs its own pair of patterns rather than a tweak
# to those. We only care about the start (first line) for the "date"/"time"
# fields; MULTIDAY_END_RE just lets the second line be recognized and
# skipped instead of leaking into the description.
_MULTIDAY_WEEKDAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_MULTIDAY_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*"
_MULTIDAY_TIME = r"\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)"
MULTIDAY_START_RE = re.compile(
    rf"^({_MULTIDAY_WEEKDAY},\s+{_MULTIDAY_MONTH}\s+\d{{1,2}})"
    rf"\s*·\s*({_MULTIDAY_TIME})\s*[–—-]\s*$"
)
MULTIDAY_END_RE = re.compile(
    rf"^{_MULTIDAY_WEEKDAY},\s+{_MULTIDAY_MONTH}\s+\d{{1,2}}\s*·\s*{_MULTIDAY_TIME}\s*$"
)
HOST_MARKER = re.compile(r"^Hosted by\s*$")
GUEST_MARKER = re.compile(r"^Guest List\s*$")
ADDR_RE = re.compile(r"\b(St\.|Ave\.|Dr\.|Blvd\.|Rd\.|Ln\.|Way|Pl\.|Pkwy\.)\b")

# Guest row: Name / <emoji> <Status> / <date>
GUEST_ROW_RE = re.compile(
    r"^([A-Z][a-zA-Z0-9 .'\-]+?)"
    r"\n[^\n]*?\s+"
    r"(Going|Maybe|Can't Go|Interested|Went)"
    r"\n(\d{1,2}/\d{1,2}/\d{4})\s*$",
    re.MULTILINE,
)

# --- Anti-detection init script ---
INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

const plugins = [
  { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
  { name: 'Chrome PDF Plugin Stream', filename: 'internal-pdf-viewer', description: 'Portable Document Format (Stream)' },
  { name: 'Chromium PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
  { name: 'Chromium PDF Plugin Stream', filename: 'internal-pdf-viewer', description: 'Portable Document Format (Stream)' },
];
Object.defineProperty(navigator, 'plugins', { get: () => plugins });

const mimeTypes = [
  { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
  { type: 'text/pdf', suffixes: 'pdf', description: 'text/pdf' },
];
Object.defineProperty(navigator, 'mimeTypes', { get: () => mimeTypes });

if (!window.chrome) {
  window.chrome = { runtime: {}, loadTimes: () => ({}) };
}
"""
