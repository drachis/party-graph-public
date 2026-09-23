"""
Self-contained event calendar.

Two independent date layers, merged by Partiful token, each drawn as its own
row of Going/Maybe/Can't Go counts on a day cell:

- RSVP layer (light colors): every row of data-request/rsvp_details.csv,
  placed on the date *you RSVP'd*. Always available, instantly, for
  everything you've ever RSVP'd to.
- Event layer (bold colors): the *confirmed* event date, placed on its own
  day once known -- from party_graph.pipeline's normal scrape (full
  venue/host/guest-list detail) or from its lightweight --gather-dates pass
  (date only, scraped_events/event_dates.json). Falls back to your own RSVP
  status when only the date (not the guest list) is known.

An event you RSVP'd to today for a party three months out shows up on
*both* its own days -- a light mark today, a bold mark on the real date --
rather than being guessed onto one date or the other. The week heatmap only
counts the event layer, so batching RSVPs ("calendar management") never
inflates it; it reflects how many confirmed parties actually land that week.

Each week card also gets a small stats line: how many RSVPs you made that
week, and (for the ones whose event date is now known) your average lead
time -- how many days ahead of the event you RSVP'd.

The page is pure HTML/CSS/JS with everything inlined -- no CDN, no plotly --
so it renders identically online or off.

Opens the result in the default browser and also saves event_calendar.html.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import re
import sys
import webbrowser
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from party_graph.config import CSV_IN, EVENT_DATES, HOST_CSV_IN, OUT_DIR, TOKEN_RE
from party_graph.utils import parse_rsvp_local_date

# Both land in OUT_DIR (scraped_events/), not next to this script -- this
# script is tool code (public repo); its output is private data (contains
# real names/RSVP status) and belongs alongside the rest of the gathered
# data, not checked into the public repo's tree.
OUT = OUT_DIR / "event_calendar.html"
DATA_OUT = OUT_DIR / "calendar_data.json"

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_ABBR = {name[:3]: i + 1 for i, name in enumerate(_MONTH_NAMES)}
_WEEKDAY_HEADERS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Matches "Monday, Dec 29, 2025" or "Saturday, Oct 24" (year omitted).
_DATE_RE = re.compile(
    r"^(?:[A-Za-z]+,\s*)?([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?$"
)

GUEST_STATUSES = ["going", "maybe", "interested", "cant_go"]
GUEST_LABELS = {
    "going": "Going", "maybe": "Maybe",
    "interested": "Interested", "cant_go": "Can't Go",
}

# The compact per-day display only has room for three buckets.
DAY_BUCKETS = ["going", "maybe", "cant_go"]

# Your own RSVP status (CSV) collapsed into the same three buckets -- used
# both for the RSVP-layer count and as the event-layer fallback when only
# the date (not the guest list) is known. HOSTING is synthetic (not a
# Partiful status) -- host_details.csv has no per-status column, you're
# just definitionally going to your own event.
CSV_STATUS_BUCKET = {
    "GOING": "going", "APPROVED": "going", "HOSTING": "going",
    "MAYBE": "maybe", "PENDING_APPROVAL": "maybe",
    "DECLINED": "cant_go", "WITHDRAWN": "cant_go",
}


def parse_event_date(raw: str | None, anchor_iso: str | None) -> date | None:
    """Parse a Partiful date string into a real date.

    Handles an explicit year ("... Dec 29, 2025") and the year-omitted form
    Partiful uses for near-term events ("Saturday, Oct 24"), in which case
    the year is inferred from anchor_iso (an ISO timestamp -- scraped_at)
    and rolled forward a year if the resulting date would be more than 90
    days in the past.
    """
    if not raw:
        return None
    m = _DATE_RE.match(raw.strip())
    if not m:
        return None
    mon_abbr, day, year = m.group(1).title(), int(m.group(2)), m.group(3)
    month = _MONTH_ABBR.get(mon_abbr)
    if not month:
        return None
    if year:
        try:
            return date(int(year), month, day)
        except ValueError:
            return None

    anchor = None
    if anchor_iso:
        try:
            anchor = datetime.fromisoformat(anchor_iso).date()
        except ValueError:
            pass
    anchor = anchor or date.today()
    try:
        candidate = date(anchor.year, month, day)
    except ValueError:
        return None
    if (anchor - candidate).days > 90:
        candidate = date(anchor.year + 1, month, day)
    return candidate


def status_bucket_counts(status: str, plus_one: int) -> dict[str, int]:
    """Collapse a CSV RSVP status (+ plus-ones) into the going/maybe/cant_go
    buckets used everywhere a full guest-list breakdown isn't available."""
    counts = {b: 0 for b in DAY_BUCKETS}
    bucket = CSV_STATUS_BUCKET.get(status)
    if bucket:
        counts[bucket] = 1 + (plus_one if bucket == "going" else 0)
    return counts


def blank_record(token: str, title: str, url: str) -> dict:
    return {
        "token": token, "title": title, "url": url,
        "rsvp_date": None, "your_status": "", "plus_one_count": 0,
        "light_counts": {b: 0 for b in DAY_BUCKETS},
        "event_date": None, "event_source": None, "cancelled": False,
        "bold_counts": {b: 0 for b in DAY_BUCKETS},
        "full_counts": {s: 0 for s in GUEST_STATUSES},
        "guests": {s: [] for s in GUEST_STATUSES},
        "time": "", "host": "", "venue": "", "address": "",
    }


def load_csv_rows(csv_path: Path) -> dict[str, dict]:
    """token -> record, RSVP-layer fields filled in from rsvp_details.csv."""
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path)
    required = {"title", "event_link", "status", "rsvp_date", "plus_one_count"}
    if not required.issubset(df.columns):
        return {}

    out: dict[str, dict] = {}
    for _, row in df.iterrows():
        url = str(row.get("event_link", ""))
        m = TOKEN_RE.search(url)
        if not m:
            continue
        rsvp_date = parse_rsvp_local_date(row.get("rsvp_date"))
        if rsvp_date is None:
            continue
        token = m.group(1)
        status = str(row.get("status") or "").strip().upper()
        plus_one = int(row.get("plus_one_count") or 0)
        rec = blank_record(token, str(row.get("title") or token), url)
        rec["rsvp_date"] = rsvp_date
        rec["your_status"] = status
        rec["plus_one_count"] = plus_one
        rec["light_counts"] = status_bucket_counts(status, plus_one)
        out[token] = rec
    return out


def load_hosted_events(csv_path: Path) -> dict[str, dict]:
    """token -> {title, url, event_date} from host_details.csv -- events
    *you* host. event_started_at is the real, authoritative event date
    (no scrape/gather needed), and hosting counts as going."""
    if not csv_path.exists():
        return {}
    df = pd.read_csv(csv_path)
    required = {"event_title", "event_link", "event_started_at"}
    if not required.issubset(df.columns):
        return {}

    out: dict[str, dict] = {}
    for _, row in df.iterrows():
        url = str(row.get("event_link", ""))
        m = TOKEN_RE.search(url)
        if not m:
            continue
        ev_date = parse_rsvp_local_date(row.get("event_started_at"))
        if ev_date is None:
            continue
        out[m.group(1)] = {
            "title": str(row.get("event_title") or m.group(1)),
            "url": url,
            "event_date": ev_date,
        }
    return out


def load_gathered_dates(path: Path) -> dict[str, dict]:
    """token -> {title, url, raw_date, raw_time, scraped_at} written by
    `scrape_events.py --gather-dates` (party_graph.pipeline.run_gather_dates)."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_scraped_events(out_dir: Path) -> tuple[list[dict], int]:
    """Return (events, skipped_count) from every scraped_events/*.json
    (the full per-event scrape: fields + guest lists)."""
    events: list[dict] = []
    skipped = 0
    for p in sorted(out_dir.glob("*.json")):
        if p.name in ("scrape_state.json", EVENT_DATES.name):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            skipped += 1
            continue
        if "guests" not in d:
            continue  # not a full per-event scrape file

        fields = d.get("fields", {}) or {}
        ev_date = parse_event_date(fields.get("date"), d.get("scraped_at"))
        if ev_date is None:
            skipped += 1
            continue

        guests = d.get("guests", {}) or {}
        title = d.get("title") or p.stem
        if title.endswith(" | Partiful"):
            # Defensive: some older saved JSON predates extract.py's own
            # suffix-stripping and still has the raw <title> tag text.
            title = title[: -len(" | Partiful")].strip()
        events.append({
            "token": d.get("token", p.stem),
            "title": title,
            "url": d.get("url", ""),
            "date": ev_date,
            "time": fields.get("time", ""),
            "host": fields.get("host", ""),
            "venue": fields.get("venue", ""),
            "address": fields.get("address", ""),
            "counts": {s: len(guests.get(s, []) or []) for s in GUEST_STATUSES},
            "guests": {
                s: [g.get("name", "") for g in (guests.get(s, []) or [])]
                for s in GUEST_STATUSES
            },
            "cancelled": bool(d.get("cancelled", False)),
        })
    return events, skipped


def build_records(csv_rows: dict[str, dict], hosted: dict[str, dict],
                   gathered: dict[str, dict], scraped_list: list[dict]) -> dict[str, dict]:
    """Merge all four sources into one record per token.

    CSV supplies the RSVP layer. host_details.csv marks a token as one you
    host -- your_status becomes the synthetic HOSTING (always "going"),
    and its event_started_at is already the real, authoritative event
    date, no scrape/gather needed. A gathered date fills in the event
    layer for anything left; a full scrape overrides it with venue/
    host/guest-list detail -- richest source wins. bold_counts (the
    compact calendar-view count) is always your own RSVP response
    (going, if hosting) regardless of source -- full_counts (the
    detail-panel guest breakdown) is the only place the real guest list
    shows up, once scraped.
    """
    records = dict(csv_rows)

    for token, h in hosted.items():
        rec = records.setdefault(token, blank_record(token, h["title"], h["url"]))
        rec["your_status"] = "HOSTING"
        rec["event_date"] = h["event_date"]
        rec["event_source"] = "hosted"
        rec["bold_counts"] = status_bucket_counts("HOSTING", 0)
        rec["full_counts"] = {**{s: 0 for s in GUEST_STATUSES}, **rec["bold_counts"]}

    for token, g in gathered.items():
        rec = records.setdefault(token, blank_record(token, g.get("title", token), g.get("url", "")))
        if rec["event_source"] == "hosted":
            continue  # host_details.csv's date is already authoritative
        ev_date = parse_event_date(g.get("raw_date"), g.get("scraped_at"))
        if ev_date is None:
            continue
        rec["event_date"] = ev_date
        rec["event_source"] = "gathered"
        rec["time"] = g.get("raw_time", "") or rec["time"]
        rec["cancelled"] = bool(g.get("cancelled", False))
        rec["bold_counts"] = status_bucket_counts(rec["your_status"], rec["plus_one_count"])
        rec["full_counts"] = {**{s: 0 for s in GUEST_STATUSES}, **rec["bold_counts"]}

    for ev in scraped_list:
        rec = records.setdefault(ev["token"], blank_record(ev["token"], ev["title"], ev["url"]))
        rec["title"] = ev["title"] or rec["title"]
        rec["url"] = ev["url"] or rec["url"]
        rec["event_date"] = ev["date"]
        rec["event_source"] = "scraped"
        rec["time"] = ev["time"]
        rec["host"] = ev["host"]
        rec["venue"] = ev["venue"]
        rec["address"] = ev["address"]
        rec["guests"] = ev["guests"]
        rec["cancelled"] = ev["cancelled"]
        rec["full_counts"] = ev["counts"]
        rec["bold_counts"] = status_bucket_counts(rec["your_status"], rec["plus_one_count"])

    return records


def build_calendar_data(records: dict[str, dict], skipped: int) -> dict:
    """Build the full JSON payload the HTML shell fetches and renders
    client-side (grid, heatmap, detail panel -- all of it). Keeping
    rendering entirely client-side means refreshing this file (fast --
    no Playwright, no HTML regeneration) is enough for the calendar to
    reflect newly gathered data; just reload the page. Mirrors what used
    to be server-rendered here (see git history for the old Python port
    of month-grid/heatmap building, now in the HTML shell's <script>).
    """
    rsvp_by_date: dict[str, list[dict]] = {}
    event_by_date: dict[str, list[dict]] = {}

    for r in records.values():
        if r["rsvp_date"]:
            rsvp_by_date.setdefault(r["rsvp_date"].isoformat(), []).append({
                "title": r["title"], "url": r["url"],
                "your_status": r["your_status"], "plus_one_count": r["plus_one_count"],
                "event_date": r["event_date"].isoformat() if r["event_date"] else None,
                "light_counts": r["light_counts"],
            })
        if r["event_date"]:
            event_by_date.setdefault(r["event_date"].isoformat(), []).append({
                "title": r["title"], "url": r["url"], "time": r["time"],
                "venue": r["venue"], "host": r["host"], "address": r["address"],
                "counts": r["full_counts"], "guests": r["guests"],
                "source": r["event_source"], "cancelled": r["cancelled"],
                "your_status": r["your_status"], "plus_one_count": r["plus_one_count"],
                "rsvp_date": r["rsvp_date"].isoformat() if r["rsvp_date"] else None,
                "bold_counts": r["bold_counts"],
            })

    n_tracked = len(records)
    n_dated = sum(1 for r in records.values() if r["event_date"])
    rsvp_dates = [r["rsvp_date"] for r in records.values() if r["rsvp_date"]]
    event_dates = [r["event_date"] for r in records.values() if r["event_date"]]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "meta": {
            "n_tracked": n_tracked,
            "n_dated": n_dated,
            "n_undated": n_tracked - n_dated,
            "n_scraped_full": sum(1 for r in records.values() if r["event_source"] == "scraped"),
            "n_gathered": sum(1 for r in records.values() if r["event_source"] == "gathered"),
            "n_hosted": sum(1 for r in records.values() if r["event_source"] == "hosted"),
            "total_going": sum(r["full_counts"].get("going", 0) for r in records.values() if r["event_date"]),
            "rsvp_range": (f"{min(rsvp_dates).isoformat()} → {max(rsvp_dates).isoformat()}"
                           if rsvp_dates else ""),
            "event_range": (f"{min(event_dates).isoformat()} → {max(event_dates).isoformat()}"
                             if event_dates else ""),
            "skipped": skipped,
        },
        "rsvp": rsvp_by_date,
        "event": event_by_date,
    }


def build_legend() -> str:
    light = "".join(f'<span class="swatch light {b}">{GUEST_LABELS[b]}</span>' for b in DAY_BUCKETS)
    bold = "".join(f'<span class="swatch bold {b}">{GUEST_LABELS[b]}</span>' for b in DAY_BUCKETS)
    return (
        '<div class="legend">'
        f'<span class="legend-label">RSVP’d that day:</span>{light}'
        f'<span class="legend-label">Event happens that day:</span>{bold}'
        '<span class="legend-note">'
        '<span class="heat-sample"></span> row shade = events you’re going/maybe '
        'going to that week (darker → busier; declined, cancelled, and RSVPs never count)</span>'
        '<span class="legend-note">'
        '<span class="cancel-mark">✕</span> = event cancelled</span>'
        '</div>'
    )


def build_html() -> str:
    """A static shell: CSS + empty containers + JS that fetches
    calendar_data.json (written by build_calendar_data(), next to this
    file) and renders everything -- grid, heatmap, detail panel -- from
    it. Regenerating just the JSON (fast, no Playwright) and reloading
    the page is enough to see new data reflected; this shell itself only
    needs regenerating when the rendering code changes.

    Needs to be served over http:// (not opened as file://) since
    fetch() of a local file is blocked by CORS in most browsers --
    `python event_calendar.py` starts a local server for this.
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Event Calendar</title>
<style>
  :root {{
    color-scheme: light;
    --bg: #f7f8fa; --card: #ffffff; --ink: #1f2430; --muted: #6b7280;
    --border: #e3e6ea; --accent: #2f6fed;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px 16px 120px;
    background: var(--bg); color: var(--ink);
    font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
  }}
  h1 {{ margin: 0 0 4px; font-size: 1.5rem; }}
  .subtitle {{ color: var(--muted); margin: 0 0 16px; font-size: 0.9rem; }}
  .summary-bar {{
    display: flex; flex-wrap: wrap; gap: 10px 22px;
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 16px; margin-bottom: 16px;
    font-size: 0.9rem;
  }}
  .summary-bar strong {{ color: var(--ink); }}
  .warning {{ color: #8a5a00; background: #fff6e0; border: 1px solid #f0d78a;
    border-radius: 8px; padding: 8px 12px; font-size: 0.85rem; margin: 0 0 8px; }}
  .warning code {{ background: rgba(0,0,0,0.06); border-radius: 4px; padding: 1px 5px; }}
  nav.month-nav {{
    position: sticky; top: 0; z-index: 5;
    display: flex; flex-wrap: wrap; gap: 6px;
    background: var(--bg); padding: 8px 0 12px;
  }}
  nav.month-nav a {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 999px; padding: 4px 12px; font-size: 0.82rem;
    color: var(--accent); text-decoration: none;
  }}
  nav.month-nav a:hover {{ border-color: var(--accent); }}
  .legend {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin: 4px 0 16px; }}
  .legend-label {{ font-size: 0.74rem; color: var(--muted); margin-left: 6px; }}
  .legend-label:first-child {{ margin-left: 0; }}
  .legend-note {{ font-size: 0.74rem; color: var(--muted); margin-left: 10px; }}
  .heat-sample {{
    display: inline-block; width: 40px; height: 12px; border-radius: 3px;
    background: linear-gradient(to right, rgb(217,242,217), rgb(253,240,189), rgb(249,199,199));
    vertical-align: middle;
  }}
  .swatch {{
    border-radius: 4px; padding: 3px 9px; font-size: 0.72rem; font-weight: 600;
  }}
  .swatch.light {{ opacity: 0.6; font-weight: 500; }}
  .swatch.going {{ background: #d9f2d9; color: #1a7a1a; }}
  .swatch.maybe {{ background: #fdf0bd; color: #8a6d00; }}
  .swatch.cant_go {{ background: #f9d7d7; color: #a10000; }}
  .months-wrap {{ display: flex; flex-wrap: wrap; gap: 14px; }}
  .month-card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px; width: 250px; max-width: 100%;
  }}
  .month-card h2 {{
    font-size: 0.88rem; margin: 0 0 8px;
    display: flex; align-items: center; gap: 6px;
  }}
  .count-pill {{
    font-size: 0.66rem; font-weight: 500; color: var(--muted);
    background: #f0f1f3; border-radius: 999px; padding: 1px 7px;
  }}
  .weekday-row {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 3px; margin-bottom: 2px; }}
  .wd {{ font-size: 0.6rem; color: var(--muted); text-align: center; padding-bottom: 2px; }}
  .weeks {{ display: flex; flex-direction: column; gap: 1px; }}
  .week-row {{
    display: grid; grid-template-columns: repeat(7, 1fr); gap: 3px;
    border-radius: 5px; padding: 2px;
  }}
  .week-stats {{
    font-size: 0.56rem; color: var(--muted); text-align: right;
    padding: 1px 4px 4px;
  }}
  .day {{
    position: relative; height: 46px; border-radius: 4px;
    background: transparent; border: 1px solid var(--border);
    padding: 2px; font-size: 0.6rem;
  }}
  .day.empty {{ background: var(--bg); border-color: transparent; }}
  .day.today {{ box-shadow: inset 0 0 0 2px var(--accent); }}
  .day.has-events {{ cursor: pointer; }}
  .day.has-events .daynum {{ color: #1a1a1a; font-weight: 600; }}
  .day.has-events:hover, .day.has-events:focus {{
    outline: none; box-shadow: 0 0 0 2px var(--accent);
  }}
  .day.selected {{ box-shadow: 0 0 0 3px #1a1a1a; }}
  .day.cancelled {{ border-style: dashed; border-color: #c94b4b; }}
  .day.cancelled .chip-row.bold span {{ text-decoration: line-through; opacity: 0.5; }}
  .daynum {{ position: absolute; top: 2px; left: 3px; color: var(--muted); }}
  .cancel-mark {{ color: #c94b4b; font-weight: 700; margin-left: 1px; }}
  .chips {{
    position: absolute; left: 2px; right: 2px; bottom: 2px;
    display: flex; flex-direction: column; gap: 1px;
  }}
  .chip-row {{ display: flex; gap: 1px; }}
  .chip-row span {{
    flex: 1; text-align: center; border-radius: 2px;
    line-height: 1.25; font-weight: 700;
  }}
  .chip-row.light span {{ font-size: 0.44rem; opacity: 0.62; font-weight: 600; }}
  .chip-row.bold span {{ font-size: 0.56rem; opacity: 1; font-weight: 800; }}
  .going {{ background: #d9f2d9; color: #1a7a1a; }}
  .maybe {{ background: #fdf0bd; color: #8a6d00; }}
  .cant_go {{ background: #f9d7d7; color: #a10000; }}
  .empty-state {{ color: var(--muted); }}
  #detail-panel {{
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 10;
    background: var(--card); border-top: 1px solid var(--border);
    box-shadow: 0 -6px 20px rgba(0,0,0,0.08);
    max-height: 42vh; overflow-y: auto;
    padding: 14px 20px 20px;
  }}
  #detail-panel.placeholder {{ color: var(--muted); font-size: 0.88rem; }}
  #detail-panel h3 {{ margin: 0 0 4px; font-size: 1rem; }}
  #detail-panel h4 {{
    margin: 12px 0 4px; font-size: 0.8rem; text-transform: uppercase;
    letter-spacing: 0.02em; color: var(--muted);
  }}
  #detail-panel .event-block {{
    padding: 8px 0; border-bottom: 1px solid var(--border);
  }}
  #detail-panel .event-block:last-child {{ border-bottom: none; }}
  #detail-panel .meta {{ color: var(--muted); font-size: 0.82rem; margin: 2px 0; }}
  #detail-panel a {{ color: var(--accent); }}
  #detail-panel .source-note {{
    color: #8a5a00; background: #fff6e0; border: 1px solid #f0d78a;
    border-radius: 6px; padding: 4px 8px; font-size: 0.78rem; margin: 4px 0;
  }}
  #detail-panel .cancelled-note {{
    color: #a10000; background: #fdeaea; border: 1px solid #f0b8b8;
    border-radius: 6px; padding: 4px 8px; font-size: 0.8rem; margin: 4px 0;
    font-weight: 600;
  }}
  .rsvp-counts {{ display: flex; gap: 12px; margin: 6px 0; font-size: 0.8rem; }}
  .rsvp-counts span {{ background: #f0f1f3; border-radius: 6px; padding: 2px 8px; }}
  details.guest-list {{ margin-top: 4px; font-size: 0.82rem; }}
  details.guest-list summary {{ cursor: pointer; color: var(--accent); }}
  details.guest-list ul {{ margin: 4px 0 0; padding-left: 18px; }}
  #close-panel {{
    position: absolute; top: 10px; right: 16px; border: none; background: none;
    font-size: 1.1rem; cursor: pointer; color: var(--muted);
  }}
</style>
</head>
<body>
  <h1>Event Calendar</h1>
  <p class="subtitle">RSVP layer from rsvp_details.csv, event layer from scraped_events/ (full scrape or --gather-dates). Loaded live from calendar_data.json -- re-run <code>python event_calendar.py --data-only</code> and reload this page to refresh.</p>

  <div id="summary-bar" class="summary-bar"></div>
  <div id="warnings"></div>

  <nav id="month-nav" class="month-nav"></nav>
  {build_legend()}

  <div id="months-wrap" class="months-wrap">
    <p class="empty-state">Loading calendar_data.json&hellip;</p>
  </div>

  <div id="detail-panel" class="placeholder">
    Click a highlighted day to see RSVP/event details.
  </div>

<script>
(function () {{
  const MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
                        "July", "August", "September", "October", "November", "December"];
  const WEEKDAY_HEADERS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const DAY_BUCKETS = ["going", "maybe", "cant_go"];

  let DATA = null;
  const panel = document.getElementById('detail-panel');
  let selectedCell = null;

  function el(tag, opts) {{
    const e = document.createElement(tag);
    if (opts) {{
      if (opts.text !== undefined) e.textContent = opts.text;
      if (opts.cls) e.className = opts.cls;
      if (opts.href) {{ e.href = opts.href; e.target = '_blank'; e.rel = 'noopener'; }}
    }}
    return e;
  }}

  // ---- date helpers (local calendar days, matching Python's date.today()
  // / rec["rsvp_date"]/rec["event_date"], which are also local-day based) ----

  function isoDate(d) {{
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
      + '-' + String(d.getDate()).padStart(2, '0');
  }}

  function addDays(d, n) {{
    const r = new Date(d);
    r.setDate(r.getDate() + n);
    return r;
  }}

  // Mon-first weeks for a month; cells outside the month are null. Mirrors
  // Python's calendar.Calendar(firstweekday=0).monthdatescalendar.
  function monthWeeks(year, month) {{
    const first = new Date(year, month - 1, 1);
    const firstWeekday = (first.getDay() + 6) % 7; // Mon=0..Sun=6
    const last = new Date(year, month, 0);
    const lastWeekday = (last.getDay() + 6) % 7;
    const end = addDays(last, 6 - lastWeekday);
    const weeks = [];
    let cur = addDays(first, -firstWeekday);
    while (cur <= end) {{
      const week = [];
      for (let i = 0; i < 7; i++) {{
        week.push(cur.getMonth() === month - 1 ? isoDate(cur) : null);
        cur = addDays(cur, 1);
      }}
      weeks.push(week);
    }}
    return weeks;
  }}

  // A confirmed event counts toward week/month heat unless it's cancelled
  // or you declined it -- heat measures events you did or might attend.
  function countsTowardHeat(rec) {{
    return !rec.cancelled && ((rec.bold_counts && rec.bold_counts.cant_go) || 0) === 0;
  }}

  // Pastel green(0) -> yellow -> red(max) wash for a week row's background.
  function weekHeatColor(value, maxValue) {{
    if (maxValue <= 0 || value <= 0) return 'transparent';
    const t = Math.max(0, Math.min(1, value / maxValue));
    const stops = [[0.0, [217, 242, 217]], [0.5, [253, 240, 189]], [1.0, [249, 199, 199]]];
    for (let i = 0; i < stops.length - 1; i++) {{
      const t0 = stops[i][0], c0 = stops[i][1], t1 = stops[i + 1][0], c1 = stops[i + 1][1];
      if (t <= t1) {{
        const k = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
        const r = Math.round(c0[0] + k * (c1[0] - c0[0]));
        const g = Math.round(c0[1] + k * (c1[1] - c0[1]));
        const b = Math.round(c0[2] + k * (c1[2] - c0[2]));
        return 'rgb(' + r + ',' + g + ',' + b + ')';
      }}
    }}
    return 'transparent';
  }}

  // (rsvp_count, avg_lead_days) for RSVPs made during this calendar week.
  function weekStats(week, rsvpByDate) {{
    let rsvpCount = 0;
    const leads = [];
    week.forEach(function (iso) {{
      if (!iso) return;
      (rsvpByDate[iso] || []).forEach(function (rec) {{
        rsvpCount += 1;
        if (rec.event_date) {{
          leads.push(Math.round((new Date(rec.event_date) - new Date(iso)) / 86400000));
        }}
      }});
    }});
    const avgLead = leads.length ? leads.reduce(function (a, b) {{ return a + b; }}, 0) / leads.length : null;
    return [rsvpCount, avgLead];
  }}

  function buildMonthCard(year, month, rsvpByDate, eventByDate, todayIso, maxWeekCount) {{
    const weeks = monthWeeks(year, month);
    let eventCount = 0;
    weeks.forEach(function (week) {{
      week.forEach(function (iso) {{
        if (!iso) return;
        (eventByDate[iso] || []).forEach(function (r) {{ if (countsTowardHeat(r)) eventCount++; }});
      }});
    }});

    const section = el('section', {{ cls: 'month-card' }});
    section.id = 'm-' + year + '-' + String(month).padStart(2, '0');
    const h2 = el('h2', {{ text: MONTH_NAMES[month - 1] + ' ' + year + ' ' }});
    h2.appendChild(el('span', {{
      cls: 'count-pill', text: eventCount + ' event' + (eventCount !== 1 ? 's' : ''),
    }}));
    section.appendChild(h2);

    const wdRow = el('div', {{ cls: 'weekday-row' }});
    WEEKDAY_HEADERS.forEach(function (h) {{ wdRow.appendChild(el('div', {{ cls: 'wd', text: h }})); }});
    section.appendChild(wdRow);

    const weeksWrap = el('div', {{ cls: 'weeks' }});
    weeks.forEach(function (week) {{
      let weekTotal = 0;
      week.forEach(function (iso) {{
        if (!iso) return;
        (eventByDate[iso] || []).forEach(function (r) {{ if (countsTowardHeat(r)) weekTotal++; }});
      }});
      const weekRow = el('div', {{ cls: 'week-row' }});
      weekRow.style.background = weekHeatColor(weekTotal, maxWeekCount);

      week.forEach(function (iso) {{
        if (!iso) {{
          weekRow.appendChild(el('div', {{ cls: 'day empty' }}));
          return;
        }}
        const rsvpList = rsvpByDate[iso] || [];
        const eventList = eventByDate[iso] || [];
        const allCancelled = eventList.length > 0 && eventList.every(function (r) {{ return r.cancelled; }});

        const day = el('div', {{ cls: 'day' }});
        day.dataset.date = iso;
        if (iso === todayIso) day.classList.add('today');
        if (rsvpList.length || eventList.length) {{
          day.classList.add('has-events');
          day.tabIndex = 0;
          day.setAttribute('role', 'button');
          if (allCancelled) {{
            day.classList.add('cancelled');
            day.title = 'This event was cancelled';
          }}
        }}

        const daynum = el('span', {{ cls: 'daynum', text: String(parseInt(iso.slice(8), 10)) }});
        if (allCancelled) daynum.appendChild(el('span', {{ cls: 'cancel-mark', text: '\\u2715' }}));
        day.appendChild(daynum);

        if (rsvpList.length || eventList.length) {{
          const chips = el('div', {{ cls: 'chips' }});
          if (rsvpList.length) {{
            const light = {{ going: 0, maybe: 0, cant_go: 0 }};
            rsvpList.forEach(function (r) {{
              DAY_BUCKETS.forEach(function (b) {{ light[b] += (r.light_counts && r.light_counts[b]) || 0; }});
            }});
            const row = el('div', {{ cls: 'chip-row light' }});
            DAY_BUCKETS.forEach(function (b) {{ row.appendChild(el('span', {{ cls: b, text: String(light[b]) }})); }});
            chips.appendChild(row);
          }}
          if (eventList.length) {{
            const bold = {{ going: 0, maybe: 0, cant_go: 0 }};
            eventList.forEach(function (r) {{
              DAY_BUCKETS.forEach(function (b) {{ bold[b] += (r.bold_counts && r.bold_counts[b]) || 0; }});
            }});
            const row = el('div', {{ cls: 'chip-row bold' }});
            DAY_BUCKETS.forEach(function (b) {{ row.appendChild(el('span', {{ cls: b, text: String(bold[b]) }})); }});
            chips.appendChild(row);
          }}
          day.appendChild(chips);
        }}
        weekRow.appendChild(day);
      }});
      weeksWrap.appendChild(weekRow);

      const stats = weekStats(week, rsvpByDate);
      const rsvpCount = stats[0], avgLead = stats[1];
      if (rsvpCount) {{
        const leadTxt = avgLead !== null ? Math.round(avgLead) + 'd avg lead' : 'lead: n/a';
        weeksWrap.appendChild(el('div', {{
          cls: 'week-stats',
          text: rsvpCount + ' RSVP' + (rsvpCount !== 1 ? 's' : '') + ' \\u00b7 ' + leadTxt,
        }}));
      }}
    }});
    section.appendChild(weeksWrap);
    return section;
  }}

  function statSpan(n, label) {{
    const s = el('span');
    s.appendChild(el('strong', {{ text: String(n) }}));
    s.appendChild(document.createTextNode(' ' + label));
    return s;
  }}

  function statSpanText(label, value) {{
    const s = el('span');
    s.appendChild(document.createTextNode(label));
    s.appendChild(el('strong', {{ text: value || 'n/a' }}));
    return s;
  }}

  function renderCalendar() {{
    const rsvpByDate = DATA.rsvp || {{}};
    const eventByDate = DATA.event || {{}};
    const meta = DATA.meta || {{}};

    const bar = document.getElementById('summary-bar');
    bar.textContent = '';
    bar.appendChild(statSpan(meta.n_tracked || 0, 'tracked event(s)'));
    bar.appendChild(statSpan(meta.n_dated || 0, 'with a confirmed date'));
    bar.appendChild(statSpan(meta.n_scraped_full || 0, 'scraped in full'));
    bar.appendChild(statSpan(meta.total_going || 0, 'going (confirmed dates)'));
    bar.appendChild(statSpanText('RSVP range: ', meta.rsvp_range));
    bar.appendChild(statSpanText('Event range: ', meta.event_range));

    const warnings = document.getElementById('warnings');
    warnings.textContent = '';
    if (meta.skipped) {{
      warnings.appendChild(el('p', {{
        cls: 'warning',
        text: meta.skipped + ' file(s) in scraped_events/ had no parseable event date and were skipped.',
      }}));
    }}
    if (meta.n_undated) {{
      warnings.appendChild(el('p', {{
        cls: 'warning',
        text: meta.n_undated + ' of ' + meta.n_tracked + ' tracked event(s) have no confirmed date yet '
          + '-- only their RSVP-day (light) mark shows. Run "python scrape_events.py --gather-dates" '
          + '(date only, never touches the guest list) or "--url <link> --now" (full detail) to fill them in.',
      }}));
    }}

    const monthsSet = {{}};
    Object.keys(rsvpByDate).concat(Object.keys(eventByDate)).forEach(function (iso) {{
      monthsSet[iso.slice(0, 7)] = true;
    }});
    const months = Object.keys(monthsSet).sort().map(function (ym) {{
      const parts = ym.split('-');
      return [parseInt(parts[0], 10), parseInt(parts[1], 10)];
    }});

    const nav = document.getElementById('month-nav');
    nav.textContent = '';
    months.forEach(function (ym) {{
      const y = ym[0], m = ym[1];
      const a = el('a', {{ text: MONTH_NAMES[m - 1].slice(0, 3) + ' ' + y }});
      a.href = '#m-' + y + '-' + String(m).padStart(2, '0');
      nav.appendChild(a);
    }});

    let maxWeekCount = 0;
    months.forEach(function (ym) {{
      monthWeeks(ym[0], ym[1]).forEach(function (week) {{
        let total = 0;
        week.forEach(function (iso) {{
          if (!iso) return;
          (eventByDate[iso] || []).forEach(function (r) {{ if (countsTowardHeat(r)) total++; }});
        }});
        if (total > maxWeekCount) maxWeekCount = total;
      }});
    }});

    const wrap = document.getElementById('months-wrap');
    wrap.textContent = '';
    if (!months.length) {{
      wrap.appendChild(el('p', {{ cls: 'empty-state', text: 'No RSVPs or events with a parseable date were found.' }}));
    }} else {{
      const todayIso = isoDate(new Date());
      months.forEach(function (ym) {{
        wrap.appendChild(buildMonthCard(ym[0], ym[1], rsvpByDate, eventByDate, todayIso, maxWeekCount));
      }});
    }}

    document.querySelectorAll('.day.has-events').forEach(function (cell) {{
      cell.addEventListener('click', function () {{ selectDay(cell); }});
      cell.addEventListener('keydown', function (e) {{
        if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); selectDay(cell); }}
      }});
    }});

    // Auto-locate: jump to (and open) the confirmed event closest to today,
    // falling back to the nearest RSVP if no event dates are known yet.
    const eventDates = Object.keys(eventByDate);
    const candidateDates = eventDates.length ? eventDates : Object.keys(rsvpByDate);
    if (candidateDates.length) {{
      const today = new Date();
      let best = candidateDates[0];
      let bestDiff = Infinity;
      candidateDates.forEach(function (iso) {{
        const diff = Math.abs(new Date(iso + 'T00:00:00') - today);
        if (diff < bestDiff) {{ bestDiff = diff; best = iso; }}
      }});
      const cell = document.querySelector('.day[data-date="' + best + '"]');
      if (cell) {{
        cell.scrollIntoView({{ behavior: 'instant', block: 'center' }});
        selectDay(cell);
      }}
    }}
  }}

  function renderEventBlock(ev) {{
    const block = el('div', {{ cls: 'event-block' }});
    const titleLine = ev.url ? el('a', {{ href: ev.url, text: ev.title }})
                              : el('strong', {{ text: ev.title }});
    const titleWrap = el('div');
    titleWrap.appendChild(titleLine);
    block.appendChild(titleWrap);

    if (ev.cancelled) {{
      block.appendChild(el('div', {{ cls: 'cancelled-note', text: '\\u2715 This event was cancelled' }}));
    }}

    const metaParts = [ev.time, ev.venue, ev.address, ev.host ? 'Host: ' + ev.host : '']
      .filter(Boolean);
    if (metaParts.length) {{
      block.appendChild(el('div', {{ cls: 'meta', text: metaParts.join(' \\u00b7 ') }}));
    }}
    if (ev.rsvp_date) {{
      block.appendChild(el('div', {{ cls: 'meta', text: 'You RSVP\\u2019d on ' + ev.rsvp_date }}));
    }}

    if (ev.source === 'gathered') {{
      block.appendChild(el('div', {{
        cls: 'source-note',
        text: 'Date confirmed, but guest list not yet scraped -- counts below are just your own RSVP.',
      }}));
    }}
    if (ev.your_status === 'HOSTING') {{
      block.appendChild(el('div', {{ cls: 'meta', text: '\\ud83c\\udf89 You\\u2019re hosting this event' }}));
    }} else if (ev.your_status) {{
      const plusOne = ev.plus_one_count ? ' (+' + ev.plus_one_count + ')' : '';
      block.appendChild(el('div', {{ cls: 'meta', text: 'Your RSVP: ' + ev.your_status + plusOne }}));
    }}

    const rsvp = el('div', {{ cls: 'rsvp-counts' }});
    [['going', 'Going'], ['maybe', 'Maybe'], ['interested', 'Interested'], ['cant_go', "Can't Go"]]
      .forEach(function (pair) {{
        const n = (ev.counts || {{}})[pair[0]] || 0;
        rsvp.appendChild(el('span', {{ text: pair[1] + ': ' + n }}));
      }});
    block.appendChild(rsvp);

    ['going', 'maybe', 'interested', 'cant_go'].forEach(function (key) {{
      const names = (ev.guests || {{}})[key] || [];
      if (!names.length) return;
      const d = document.createElement('details');
      d.className = 'guest-list';
      const s = document.createElement('summary');
      s.textContent = names.length + ' ' + key.replace('_', ' ') + ' guest(s)';
      d.appendChild(s);
      const ul = document.createElement('ul');
      names.forEach(function (name) {{ ul.appendChild(el('li', {{ text: name }})); }});
      d.appendChild(ul);
      block.appendChild(d);
    }});
    return block;
  }}

  function renderRsvpBlock(r) {{
    const block = el('div', {{ cls: 'event-block' }});
    const titleLine = r.url ? el('a', {{ href: r.url, text: r.title }})
                             : el('strong', {{ text: r.title }});
    const titleWrap = el('div');
    titleWrap.appendChild(titleLine);
    block.appendChild(titleWrap);

    const plusOne = r.plus_one_count ? ' (+' + r.plus_one_count + ')' : '';
    block.appendChild(el('div', {{ cls: 'meta', text: 'Your RSVP: ' + r.your_status + plusOne }}));
    block.appendChild(el('div', {{
      cls: 'meta',
      text: r.event_date ? 'Happens ' + r.event_date : 'Event date not yet known',
    }}));
    return block;
  }}

  function renderDay(iso) {{
    const rsvps = DATA.rsvp[iso] || [];
    const events = DATA.event[iso] || [];
    panel.innerHTML = '';
    panel.classList.remove('placeholder');

    const closeBtn = el('button', {{ text: '\\u2715' }});
    closeBtn.id = 'close-panel';
    closeBtn.onclick = clearSelection;
    panel.appendChild(closeBtn);

    panel.appendChild(el('h3', {{ text: new Date(iso + 'T00:00:00').toDateString() }}));

    if (!rsvps.length && !events.length) {{
      panel.appendChild(el('div', {{ cls: 'meta', text: 'Nothing on this day.' }}));
      return;
    }}

    if (events.length) {{
      panel.appendChild(el('h4', {{ text: 'Event' + (events.length > 1 ? 's' : '') + ' happening this day' }}));
      events.forEach(function (ev) {{ panel.appendChild(renderEventBlock(ev)); }});
    }}
    if (rsvps.length) {{
      panel.appendChild(el('h4', {{ text: 'RSVP' + (rsvps.length > 1 ? 's' : '') + ' made this day' }}));
      rsvps.forEach(function (r) {{ panel.appendChild(renderRsvpBlock(r)); }});
    }}
  }}

  function clearSelection() {{
    if (selectedCell) selectedCell.classList.remove('selected');
    selectedCell = null;
    panel.classList.add('placeholder');
    panel.textContent = 'Click a highlighted day to see RSVP/event details.';
  }}

  function selectDay(cell) {{
    if (selectedCell) selectedCell.classList.remove('selected');
    selectedCell = cell;
    cell.classList.add('selected');
    renderDay(cell.dataset.date);
  }}

  fetch('calendar_data.json')
    .then(function (r) {{
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }})
    .then(function (json) {{
      DATA = json;
      renderCalendar();
    }})
    .catch(function (err) {{
      document.getElementById('months-wrap').textContent =
        'Failed to load calendar_data.json: ' + err.message
        + ' -- make sure you\\'re viewing this over http:// (not file://) and that '
        + 'calendar_data.json is next to this file. `python event_calendar.py` starts a local server for this.';
    }});
}})();
</script>
</body>
</html>
"""


def serve(directory: Path, port: int) -> None:
    """Local-only static server for the shell + its JSON -- fetch() of a
    calendar_data.json is blocked by CORS when opened as file://, so this
    is what makes viewing it actually work. Picks a free port unless one
    is given. Blocks (Ctrl+C to stop); re-running with --data-only in
    another terminal updates calendar_data.json without disturbing this."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/event_calendar.html"
        print(f"Serving {directory}/ at {url}  (Ctrl+C to stop)")
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the private event calendar from local data.")
    ap.add_argument("--data-only", action="store_true",
                     help="only refresh calendar_data.json (fast, no Playwright) -- "
                          "use this to pick up newly gathered data without disturbing "
                          "an already-running --serve / already-open browser tab; just reload it")
    ap.add_argument("--no-serve", action="store_true",
                     help="write the files but don't start a local server or open a browser")
    ap.add_argument("--port", type=int, default=0,
                     help="local server port (default: let the OS pick a free one)")
    args = ap.parse_args()

    if not CSV_IN.exists() and not OUT_DIR.exists():
        sys.exit(f"Neither {CSV_IN} nor {OUT_DIR}/ was found -- nothing to build a calendar from.")

    csv_rows = load_csv_rows(CSV_IN)
    hosted = load_hosted_events(HOST_CSV_IN)
    gathered = load_gathered_dates(EVENT_DATES)
    scraped_list, skipped = load_scraped_events(OUT_DIR) if OUT_DIR.exists() else ([], 0)
    records = build_records(csv_rows, hosted, gathered, scraped_list)

    if not records:
        print("No RSVPs or events found.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = build_calendar_data(records, skipped)
    DATA_OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    meta = data["meta"]
    print(f"Wrote {DATA_OUT}")
    print(f"{meta['n_tracked']} tracked event(s), {meta['n_dated']} with a confirmed date "
          f"({meta['n_scraped_full']} scraped in full, {meta['n_gathered']} date-only, "
          f"{meta['n_hosted']} hosted by you).")
    if skipped:
        print(f"WARNING: {skipped} file(s) in {OUT_DIR}/ had no parseable event date and were skipped.")

    if args.data_only:
        return

    OUT.write_text(build_html(), encoding="utf-8")
    print(f"Wrote {OUT}")

    if args.no_serve:
        print(f"Open {OUT} yourself over http:// -- file:// won't work, fetch() of "
              "calendar_data.json needs a server (e.g. `python -m http.server` from scraped_events/).")
        return

    serve(OUT_DIR, args.port)


if __name__ == "__main__":
    main()
