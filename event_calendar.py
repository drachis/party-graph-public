"""
Self-contained event calendar.

Populates the calendar in two layers, merged by Partiful event token:

1. data-request/rsvp_details.csv -- every event you've ever RSVP'd to, gives
   instant (if approximate) coverage. Its 'rsvp_date' is when *you* RSVP'd,
   not the event date, so these entries are placed on that date and flagged
   "estimated" until the real thing is scraped.
2. scraped_events/*.json (written by party_graph.pipeline) -- once a token
   is scraped, its entry is replaced wholesale with the confirmed date,
   venue, host, and full per-status guest lists.

Each day with an event is colored/sized by event count on a green (fewest)
-> red (most) scale; clicking a highlighted day opens a detail panel with
venue, host, RSVP counts, and guest names pulled straight from the scraped
data (or your own RSVP status, for days not yet scraped).

The page is pure HTML/CSS/JS with everything inlined -- no CDN, no plotly --
so it renders identically online or off.

Opens the result in the default browser and also saves event_calendar.html.
"""

from __future__ import annotations

import html
import json
import re
import sys
import webbrowser
from calendar import Calendar
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from party_graph.config import CSV_IN, OUT_DIR, TOKEN_RE

OUT = Path(__file__).parent / "event_calendar.html"

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
BUCKET_COLOR = {"going": "#1a7a1a", "maybe": "#8a6d00", "cant_go": "#a10000"}
BUCKET_BG = {"going": "#d9f2d9", "maybe": "#fdf0bd", "cant_go": "#f9d7d7"}

# Your own RSVP status (CSV) collapsed into the same three buckets, so a
# not-yet-scraped event still shows a meaningful (if 1-person) breakdown.
CSV_STATUS_BUCKET = {
    "GOING": "going", "APPROVED": "going",
    "MAYBE": "maybe", "PENDING_APPROVAL": "maybe",
    "DECLINED": "cant_go", "WITHDRAWN": "cant_go",
}


def parse_event_date(raw: str | None, scraped_at: str | None) -> date | None:
    """Parse fields['date'] into a real date.

    Handles an explicit year ("... Dec 29, 2025") and the year-omitted form
    Partiful uses for near-term events ("Saturday, Oct 24"), in which case
    the year is inferred from scraped_at and rolled forward a year if the
    resulting date would be more than 90 days in the past.
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
    if scraped_at:
        try:
            anchor = datetime.fromisoformat(scraped_at).date()
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


def parse_rsvp_timestamp(raw: object) -> date | None:
    """Parse the CSV's 'rsvp_date', e.g. 'Sep 08, 2023 03:51:32 PM UTC'."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith(" UTC"):
        s = s[: -len(" UTC")]
    try:
        return datetime.strptime(s, "%b %d, %Y %I:%M:%S %p").date()
    except ValueError:
        return None


def load_csv_events(csv_path: Path) -> dict[str, dict]:
    """Return {token: event} from rsvp_details.csv, keyed by Partiful token.

    Placeholder entries only: date is your RSVP timestamp (not the event
    date), so there's no venue/host/guest breakdown yet -- just your own
    status and plus-one count. Marked estimated=True; merge_events()
    replaces any token also present in scraped_events/ with the real thing.
    """
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
        ev_date = parse_rsvp_timestamp(row.get("rsvp_date"))
        if ev_date is None:
            continue
        token = m.group(1)
        status = str(row.get("status") or "").strip().upper()
        plus_one = int(row.get("plus_one_count") or 0)
        counts = {s: 0 for s in GUEST_STATUSES}
        bucket = CSV_STATUS_BUCKET.get(status)
        if bucket:
            counts[bucket] = 1 + (plus_one if bucket == "going" else 0)
        out[token] = {
            "token": token,
            "title": str(row.get("title") or token),
            "url": url,
            "date": ev_date,
            "time": "",
            "host": "",
            "venue": "",
            "address": "",
            "counts": counts,
            "guests": {s: [] for s in GUEST_STATUSES},
            "estimated": True,
            "your_status": status,
            "plus_one_count": plus_one,
        }
    return out


def merge_events(csv_events: dict[str, dict], scraped_events: list[dict]) -> list[dict]:
    """CSV rows populate the calendar first; a scraped entry for the same
    token replaces it wholesale (confirmed date/venue/guests), carrying
    forward the CSV's own RSVP status/plus-one if the scrape doesn't have it
    (the scraped guest list is other guests, not your own RSVP)."""
    merged = dict(csv_events)
    for ev in scraped_events:
        prior = merged.get(ev["token"], {})
        ev = dict(ev)
        ev["your_status"] = ev.get("your_status") or prior.get("your_status", "")
        ev["plus_one_count"] = ev.get("plus_one_count") or prior.get("plus_one_count", 0)
        merged[ev["token"]] = ev
    return list(merged.values())


def load_events(out_dir: Path) -> tuple[list[dict], int]:
    """Return (events, skipped_count) from every scraped_events/*.json."""
    events: list[dict] = []
    skipped = 0
    for p in sorted(out_dir.glob("*.json")):
        if p.name == "scrape_state.json":
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            skipped += 1
            continue

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
            "estimated": False,
            "your_status": "",
            "plus_one_count": 0,
        })
    return events, skipped


def month_weeks(year: int, month: int) -> list[list[date | None]]:
    """Weeks (Mon-first) for a month, with out-of-month days as None."""
    cal = Calendar(firstweekday=0)
    return [
        [d if d.month == month else None for d in week]
        for week in cal.monthdatescalendar(year, month)
    ]


def week_heat_color(value: int, max_value: int) -> str:
    """Pastel green(0) -> yellow -> red(max) wash for a week row's background.

    Uses the same three hues as the per-day Going/Maybe/Can't Go chips, just
    lighter, so a busy week reads as a soft red band and a quiet one as soft
    green, without drowning out the day numbers/chips drawn on top.
    """
    if max_value <= 0 or value <= 0:
        return "transparent"
    t = max(0.0, min(1.0, value / max_value))
    stops = [(0.0, (217, 242, 217)), (0.5, (253, 240, 189)), (1.0, (249, 199, 199))]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            r = round(c0[0] + k * (c1[0] - c0[0]))
            g = round(c0[1] + k * (c1[1] - c0[1]))
            b = round(c0[2] + k * (c1[2] - c0[2]))
            return f"rgb({r},{g},{b})"
    return "transparent"


def build_month_card(year: int, month: int, day_events: dict[date, list[dict]],
                      today: date, max_week_count: int) -> str:
    label = f"{_MONTH_NAMES[month - 1]} {year}"
    weeks = month_weeks(year, month)
    count = sum(len(day_events.get(d, [])) for week in weeks for d in week if d)
    anchor = f"m-{year}-{month:02d}"

    header_cells = "".join(f'<div class="wd">{h}</div>' for h in _WEEKDAY_HEADERS)

    week_rows = []
    for week in weeks:
        week_total = sum(len(day_events.get(d, [])) for d in week if d)
        heat = week_heat_color(week_total, max_week_count)

        day_cells = []
        for d in week:
            if d is None:
                day_cells.append('<div class="day empty"></div>')
                continue
            evs = day_events.get(d, [])
            n = len(evs)
            classes = "day"
            attrs = f'data-date="{d.isoformat()}"'
            if d == today:
                classes += " today"
            gmd = ""
            if n:
                classes += " has-events"
                if all(ev["estimated"] for ev in evs):
                    classes += " estimated"
                attrs += ' tabindex="0" role="button"'
                totals = {b: sum(ev["counts"][b] for ev in evs) for b in DAY_BUCKETS}
                gmd = '<div class="gmd">' + "".join(
                    f'<span class="{b}">{totals[b]}</span>' for b in DAY_BUCKETS
                ) + '</div>'
            day_cells.append(
                f'<div class="{classes}" {attrs}>'
                f'<span class="daynum">{d.day}</span>{gmd}</div>'
            )
        week_rows.append(
            f'<div class="week-row" style="background:{heat}">'
            f'{"".join(day_cells)}</div>'
        )

    return (
        f'<section class="month-card" id="{anchor}">'
        f'<h2>{html.escape(label)} <span class="count-pill">{count} event'
        f'{"s" if count != 1 else ""}</span></h2>'
        f'<div class="weekday-row">{header_cells}</div>'
        f'<div class="weeks">{"".join(week_rows)}</div>'
        f'</section>'
    )


def build_legend() -> str:
    swatches = "".join(
        f'<span class="swatch {b}">{GUEST_LABELS[b]}</span>' for b in DAY_BUCKETS
    )
    return (
        '<div class="legend">'
        f'{swatches}'
        '<span class="legend-note">'
        '<span class="dash-sample"></span> dashed border = estimated from '
        'your RSVP date, not yet scraped</span>'
        '<span class="legend-note">'
        '<span class="heat-sample"></span> row shade = events that week '
        '(darker → busier)</span>'
        '</div>'
    )


def build_html(events: list[dict], skipped: int) -> str:
    today = date.today()
    day_events: dict[date, list[dict]] = {}
    for ev in events:
        day_events.setdefault(ev["date"], []).append(ev)

    months = sorted({(d.year, d.month) for d in day_events})
    max_week_count = max(
        (sum(len(day_events.get(d, [])) for d in week if d)
         for y, m in months for week in month_weeks(y, m)),
        default=0,
    )

    total_going = sum(ev["counts"]["going"] for ev in events)
    n_estimated = sum(1 for ev in events if ev["estimated"])
    n_confirmed = len(events) - n_estimated
    date_range = ""
    if events:
        lo, hi = min(day_events), max(day_events)
        date_range = f"{lo.isoformat()} → {hi.isoformat()}"

    nav_links = "".join(
        f'<a href="#m-{y}-{m:02d}">{_MONTH_NAMES[m - 1][:3]} {y}</a>'
        for y, m in months
    )

    month_cards = "".join(
        build_month_card(y, m, day_events, today, max_week_count) for y, m in months
    )

    if not months:
        month_cards = '<p class="empty-state">No events with a parseable date were found.</p>'

    # Data for the detail panel: keyed by ISO date, escaped/serialized as JSON
    # and read back via textContent (never innerHTML on raw strings), so
    # scraped guest/venue text can't inject markup.
    events_by_date = {
        d.isoformat(): [
            {
                "title": ev["title"],
                "url": ev["url"],
                "time": ev["time"],
                "host": ev["host"],
                "venue": ev["venue"],
                "address": ev["address"],
                "counts": ev["counts"],
                "guests": ev["guests"],
                "estimated": ev["estimated"],
                "your_status": ev["your_status"],
                "plus_one_count": ev["plus_one_count"],
            }
            for ev in evs
        ]
        for d, evs in day_events.items()
    }
    data_json = json.dumps(events_by_date, ensure_ascii=False).replace("</", "<\\/")

    warning = ""
    if skipped:
        warning = (
            f'<p class="warning">{skipped} file(s) had no parseable event date '
            "and were skipped.</p>"
        )

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
    border-radius: 8px; padding: 8px 12px; font-size: 0.85rem; }}
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
  .legend-note {{ font-size: 0.74rem; color: var(--muted); margin-left: 8px; }}
  .dash-sample {{
    display: inline-block; width: 16px; height: 12px; border-radius: 3px;
    border: 1.5px dashed #9aa1ab; vertical-align: middle;
  }}
  .heat-sample {{
    display: inline-block; width: 40px; height: 12px; border-radius: 3px;
    background: linear-gradient(to right, rgb(217,242,217), rgb(253,240,189), rgb(249,199,199));
    vertical-align: middle;
  }}
  .swatch {{
    border-radius: 4px; padding: 3px 9px; font-size: 0.72rem; font-weight: 600;
  }}
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
  .weeks {{ display: flex; flex-direction: column; gap: 3px; }}
  .week-row {{
    display: grid; grid-template-columns: repeat(7, 1fr); gap: 3px;
    border-radius: 5px; padding: 2px;
  }}
  .day {{
    position: relative; height: 38px; border-radius: 4px;
    background: transparent; border: 1px solid var(--border);
    padding: 2px; font-size: 0.6rem;
  }}
  .day.empty {{ background: var(--bg); border-color: transparent; }}
  .day.today {{ box-shadow: inset 0 0 0 2px var(--accent); }}
  .day.has-events {{ cursor: pointer; }}
  .day.has-events .daynum {{ color: #1a1a1a; font-weight: 600; }}
  .day.estimated {{ border-style: dashed; border-color: #9aa1ab; }}
  .day.has-events:hover, .day.has-events:focus {{
    outline: none; box-shadow: 0 0 0 2px var(--accent);
  }}
  .day.selected {{ box-shadow: 0 0 0 3px #1a1a1a; }}
  .daynum {{ position: absolute; top: 2px; left: 3px; color: var(--muted); }}
  .gmd {{
    position: absolute; left: 2px; right: 2px; bottom: 2px;
    display: flex; gap: 1px;
  }}
  .gmd span {{
    flex: 1; text-align: center; border-radius: 3px;
    font-size: 0.5rem; line-height: 1.3; font-weight: 700;
  }}
  .gmd .going {{ background: #d9f2d9; color: #1a7a1a; }}
  .gmd .maybe {{ background: #fdf0bd; color: #8a6d00; }}
  .gmd .cant_go {{ background: #f9d7d7; color: #a10000; }}
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
  #detail-panel .event-block {{
    padding: 10px 0; border-bottom: 1px solid var(--border);
  }}
  #detail-panel .event-block:last-child {{ border-bottom: none; }}
  #detail-panel .meta {{ color: var(--muted); font-size: 0.82rem; margin: 2px 0; }}
  #detail-panel a {{ color: var(--accent); }}
  #detail-panel .estimated-note {{
    color: #8a5a00; background: #fff6e0; border: 1px solid #f0d78a;
    border-radius: 6px; padding: 4px 8px; font-size: 0.78rem; margin: 4px 0;
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
  <p class="subtitle">Generated from rsvp_details.csv, enriched by scraped_events/*.json</p>

  <div class="summary-bar">
    <span><strong>{len(events)}</strong> event(s)</span>
    <span><strong>{n_confirmed}</strong> scraped in detail</span>
    <span><strong>{n_estimated}</strong> estimated from RSVP history</span>
    <span><strong>{total_going}</strong> total going</span>
    <span>Range: <strong>{date_range or "n/a"}</strong></span>
  </div>
  {warning}

  <nav class="month-nav">{nav_links}</nav>
  {build_legend()}

  <div class="months-wrap">{month_cards}</div>

  <div id="detail-panel" class="placeholder">
    Click a highlighted day to see event details.
  </div>

<script id="event-data" type="application/json">{data_json}</script>
<script>
(function () {{
  const DATA = JSON.parse(document.getElementById('event-data').textContent);
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

  function renderDay(iso) {{
    const events = DATA[iso] || [];
    panel.innerHTML = '';
    panel.classList.remove('placeholder');

    const closeBtn = el('button', {{ text: '\\u2715' }});
    closeBtn.id = 'close-panel';
    closeBtn.onclick = clearSelection;
    panel.appendChild(closeBtn);

    const heading = el('h3', {{ text: new Date(iso + 'T00:00:00').toDateString() }});
    panel.appendChild(heading);

    if (!events.length) {{
      panel.appendChild(el('div', {{ cls: 'meta', text: 'No events on this day.' }}));
      return;
    }}

    events.forEach(function (ev) {{
      const block = el('div', {{ cls: 'event-block' }});

      const titleLine = ev.url ? el('a', {{ href: ev.url, text: ev.title }})
                                : el('strong', {{ text: ev.title }});
      const titleWrap = el('div');
      titleWrap.appendChild(titleLine);
      block.appendChild(titleWrap);

      const metaParts = [ev.time, ev.venue, ev.address, ev.host ? 'Host: ' + ev.host : '']
        .filter(Boolean);
      if (metaParts.length) {{
        block.appendChild(el('div', {{ cls: 'meta', text: metaParts.join(' \\u00b7 ') }}));
      }}

      if (ev.estimated) {{
        block.appendChild(el('div', {{
          cls: 'estimated-note',
          text: 'Date estimated from your RSVP timestamp \\u2014 run the scraper for the confirmed date/venue/guests.',
        }}));
      }}
      if (ev.your_status) {{
        const plusOne = ev.plus_one_count ? ' (+' + ev.plus_one_count + ')' : '';
        block.appendChild(el('div', {{ cls: 'meta', text: 'Your RSVP: ' + ev.your_status + plusOne }}));
      }}

      if (!ev.estimated) {{
        const rsvp = el('div', {{ cls: 'rsvp-counts' }});
        [['going', 'Going'], ['maybe', 'Maybe'], ['interested', 'Interested'], ['cant_go', "Can't Go"]]
          .forEach(function (pair) {{
            const n = (ev.counts || {{}})[pair[0]] || 0;
            rsvp.appendChild(el('span', {{ text: pair[1] + ': ' + n }}));
          }});
        block.appendChild(rsvp);
      }}

      ['going', 'maybe', 'interested', 'cant_go'].forEach(function (key) {{
        const names = (ev.guests || {{}})[key] || [];
        if (!names.length) return;
        const d = document.createElement('details');
        d.className = 'guest-list';
        const s = document.createElement('summary');
        s.textContent = names.length + ' ' + key.replace('_', ' ') + ' guest(s)';
        d.appendChild(s);
        const ul = document.createElement('ul');
        names.forEach(function (name) {{
          ul.appendChild(el('li', {{ text: name }}));
        }});
        d.appendChild(ul);
        block.appendChild(d);
      }});

      panel.appendChild(block);
    }});
  }}

  function clearSelection() {{
    if (selectedCell) selectedCell.classList.remove('selected');
    selectedCell = null;
    panel.classList.add('placeholder');
    panel.textContent = 'Click a highlighted day to see event details.';
  }}

  function selectDay(cell) {{
    if (selectedCell) selectedCell.classList.remove('selected');
    selectedCell = cell;
    cell.classList.add('selected');
    renderDay(cell.dataset.date);
  }}

  document.querySelectorAll('.day.has-events').forEach(function (cell) {{
    cell.addEventListener('click', function () {{ selectDay(cell); }});
    cell.addEventListener('keydown', function (e) {{
      if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); selectDay(cell); }}
    }});
  }});

  // Auto-locate: jump to (and open) the event closest to today.
  const isoDates = Object.keys(DATA);
  if (isoDates.length) {{
    const today = new Date();
    let best = isoDates[0];
    let bestDiff = Infinity;
    isoDates.forEach(function (iso) {{
      const diff = Math.abs(new Date(iso + 'T00:00:00') - today);
      if (diff < bestDiff) {{ bestDiff = diff; best = iso; }}
    }});
    const cell = document.querySelector('.day[data-date="' + best + '"]');
    if (cell) {{
      cell.scrollIntoView({{ behavior: 'instant', block: 'center' }});
      selectDay(cell);
    }}
  }}
}})();
</script>
</body>
</html>
"""


def main() -> None:
    if not CSV_IN.exists() and not OUT_DIR.exists():
        sys.exit(f"Neither {CSV_IN} nor {OUT_DIR}/ was found -- nothing to build a calendar from.")

    csv_events = load_csv_events(CSV_IN)
    scraped_events, skipped = load_events(OUT_DIR) if OUT_DIR.exists() else ([], 0)
    events = merge_events(csv_events, scraped_events)

    if not events:
        print("No events with a parseable date found.")
        if skipped:
            print(f"({skipped} file(s) in {OUT_DIR}/ skipped: unparseable or missing date.)")

    html_doc = build_html(events, skipped)
    OUT.write_text(html_doc, encoding="utf-8")
    webbrowser.open(OUT.resolve().as_uri())

    n_estimated = sum(1 for ev in events if ev["estimated"])
    print(f"Wrote {OUT} and opened in browser.")
    print(f"{len(events)} event(s) across {len({e['date'] for e in events})} day(s) "
          f"({len(events) - n_estimated} scraped in detail, {n_estimated} estimated from RSVP history).")
    if skipped:
        print(f"WARNING: {skipped} file(s) in {OUT_DIR}/ had no parseable event date and were skipped.")


if __name__ == "__main__":
    main()
