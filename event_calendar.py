"""
Self-contained event calendar.

Reads scraped_events/*.json (written by party_graph.pipeline) and renders a
month-by-month heatmap calendar of event dates. Each day with an event is
colored/sized by event count on a green (fewest) -> red (most) scale; clicking
a highlighted day opens a detail panel with venue, host, RSVP counts, and
guest names pulled straight from the scraped data.

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

from party_graph.config import OUT_DIR

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
        })
    return events, skipped


def color_for(value: int, max_value: int) -> str:
    """rgb() on a green(0) -> yellow -> red(max) scale."""
    if max_value <= 0 or value <= 0:
        return "rgb(232,236,239)"
    t = max(0.0, min(1.0, value / max_value))
    if t < 0.5:
        k = t / 0.5
        r = int(144 + k * (255 - 144))
        g = int(238 + k * (255 - 238))
        b = int(144 + k * (0 - 144))
    else:
        k = (t - 0.5) / 0.5
        r = int(255 + k * (220 - 255))
        g = int(255 + k * (20 - 255))
        b = int(0 + k * (60 - 0))
    return f"rgb({r},{g},{b})"


def month_weeks(year: int, month: int) -> list[list[date | None]]:
    """Weeks (Mon-first) for a month, with out-of-month days as None."""
    cal = Calendar(firstweekday=0)
    return [
        [d if d.month == month else None for d in week]
        for week in cal.monthdatescalendar(year, month)
    ]


def build_month_card(year: int, month: int, day_events: dict[date, list[dict]],
                      max_count: int, today: date) -> str:
    label = f"{_MONTH_NAMES[month - 1]} {year}"
    count = sum(len(day_events.get(d, [])) for week in month_weeks(year, month)
                for d in week if d)
    anchor = f"m-{year}-{month:02d}"

    header_cells = "".join(f'<div class="wd">{h}</div>' for h in _WEEKDAY_HEADERS)

    day_cells = []
    for week in month_weeks(year, month):
        for d in week:
            if d is None:
                day_cells.append('<div class="day empty"></div>')
                continue
            evs = day_events.get(d, [])
            n = len(evs)
            classes = "day"
            style = ""
            attrs = f'data-date="{d.isoformat()}"'
            if d == today:
                classes += " today"
            if n:
                classes += " has-events"
                bg = color_for(n, max_count)
                style = f' style="background:{bg}"'
                attrs += ' tabindex="0" role="button"'
            badge = f'<span class="badge">{n}</span>' if n else ""
            day_cells.append(
                f'<div class="{classes}"{style} {attrs}>'
                f'<span class="daynum">{d.day}</span>{badge}</div>'
            )

    grid = "".join(day_cells)
    return (
        f'<section class="month-card" id="{anchor}">'
        f'<h2>{html.escape(label)} <span class="count-pill">{count} event'
        f'{"s" if count != 1 else ""}</span></h2>'
        f'<div class="grid">{header_cells}{grid}</div>'
        f'</section>'
    )


def build_legend(max_count: int) -> str:
    steps = 5
    swatches = []
    for i in range(steps + 1):
        v = round(max_count * i / steps) if max_count else 0
        swatches.append(
            f'<div class="swatch" style="background:{color_for(v, max_count)}">'
            f'{v}</div>'
        )
    return (
        '<div class="legend">'
        '<span class="legend-label">Fewer events</span>'
        f'{"".join(swatches)}'
        '<span class="legend-label">More events</span>'
        '</div>'
    )


def build_html(events: list[dict], skipped: int) -> str:
    today = date.today()
    day_events: dict[date, list[dict]] = {}
    for ev in events:
        day_events.setdefault(ev["date"], []).append(ev)

    max_count = max((len(v) for v in day_events.values()), default=0)
    months = sorted({(d.year, d.month) for d in day_events})

    total_going = sum(ev["counts"]["going"] for ev in events)
    date_range = ""
    if events:
        lo, hi = min(day_events), max(day_events)
        date_range = f"{lo.isoformat()} → {hi.isoformat()}"

    nav_links = "".join(
        f'<a href="#m-{y}-{m:02d}">{_MONTH_NAMES[m - 1][:3]} {y}</a>'
        for y, m in months
    )

    month_cards = "".join(
        build_month_card(y, m, day_events, max_count, today) for y, m in months
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
  .legend {{ display: flex; align-items: center; gap: 6px; margin: 4px 0 18px; }}
  .legend-label {{ font-size: 0.78rem; color: var(--muted); }}
  .swatch {{
    width: 30px; height: 20px; border-radius: 4px; border: 1px solid rgba(0,0,0,0.08);
    display: flex; align-items: center; justify-content: center;
    font-size: 0.68rem; color: rgba(0,0,0,0.55);
  }}
  .month-card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 16px; margin-bottom: 20px;
    max-width: 620px;
  }}
  .month-card h2 {{
    font-size: 1.05rem; margin: 0 0 10px;
    display: flex; align-items: center; gap: 8px;
  }}
  .count-pill {{
    font-size: 0.72rem; font-weight: 500; color: var(--muted);
    background: #f0f1f3; border-radius: 999px; padding: 2px 9px;
  }}
  .grid {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }}
  .wd {{ font-size: 0.7rem; color: var(--muted); text-align: center; padding-bottom: 4px; }}
  .day {{
    position: relative; aspect-ratio: 1; border-radius: 8px;
    background: #fbfbfc; border: 1px solid var(--border);
    padding: 4px 6px; font-size: 0.75rem;
  }}
  .day.empty {{ background: transparent; border-color: transparent; }}
  .day.today {{ box-shadow: inset 0 0 0 2px var(--accent); }}
  .day.has-events {{ cursor: pointer; color: #1a1a1a; font-weight: 600; }}
  .day.has-events:hover, .day.has-events:focus {{
    outline: none; box-shadow: 0 0 0 2px var(--accent);
  }}
  .day.selected {{ box-shadow: 0 0 0 3px #1a1a1a; }}
  .daynum {{ position: absolute; top: 4px; left: 6px; }}
  .badge {{
    position: absolute; bottom: 3px; right: 5px;
    font-size: 0.68rem; opacity: 0.85;
  }}
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
  <p class="subtitle">Generated from scraped_events/*.json</p>

  <div class="summary-bar">
    <span><strong>{len(events)}</strong> event(s)</span>
    <span><strong>{len(day_events)}</strong> day(s) with events</span>
    <span><strong>{total_going}</strong> total going</span>
    <span>Range: <strong>{date_range or "n/a"}</strong></span>
  </div>
  {warning}

  <nav class="month-nav">{nav_links}</nav>
  {build_legend(max_count)}

  {month_cards}

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
    if not OUT_DIR.exists():
        sys.exit(f"No {OUT_DIR}/ directory found -- run the scraper first.")

    events, skipped = load_events(OUT_DIR)
    if not events:
        print(f"No events with a parseable date found in {OUT_DIR}/.")
        if skipped:
            print(f"({skipped} file(s) skipped: unparseable or missing date.)")

    html_doc = build_html(events, skipped)
    OUT.write_text(html_doc, encoding="utf-8")
    webbrowser.open(OUT.resolve().as_uri())

    print(f"Wrote {OUT} and opened in browser.")
    print(f"{len(events)} event(s) across "
          f"{len({e['date'] for e in events})} day(s).")
    if skipped:
        print(f"WARNING: {skipped} file(s) had no parseable event date and were skipped.")


if __name__ == "__main__":
    main()
