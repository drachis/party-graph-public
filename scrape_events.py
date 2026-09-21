"""Partiful event-link scraper with daily budget, evening window, and auth.

Usage:
    python scrape_events.py                 # run today's budget
    python scrape_events.py --limit 2       # cap today's visits at 2
    python scrape_events.py --fresh         # ignore saved files, re-scrape

Notes:
- First run will open a headed browser. Log in to Partiful once; the
  session is stored in data-request/.browser/ and reused on later runs.
- A daily budget ledger (data-request/scrape_state.json) enforces
  MIN_DAILY..MAX_DAILY visits per calendar day, spread over ~SPREAD_HOURS
  starting at WINDOW_START.
- Already-saved tokens are skipped without consuming budget.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
import urllib.request
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright

# --- config ---
CSV_IN = Path("data-request/rsvp_details.csv")
OUT_DIR = Path("scraped_events")
SUMMARY = Path("scraped_events/summary.csv")
STATE = Path("scraped_events/scrape_state.json")
USER_DATA_DIR = Path("scraped_events/.browser")

MIN_DAILY = 8
MAX_DAILY = 12
SPREAD_HOURS = 1.0
WINDOW_START = "19:00"
WINDOW_END = "23:00"
HEADLESS = False

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]
BLOCK_SELECTORS = ["nav", "footer", "header", "[role='banner']", "[role='navigation']", "script", "style", "noscript", "svg"]
FIELD_RE = re.compile(r"^\s*(Host|Hosts|Location|Venue|Address|Date|Time|Date & Time|Description|Notes|Contact|Organizer|Price|Capacity)\s*[:\-–—]\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
TOKEN_RE = re.compile(r"partiful\.com/e/([A-Za-z0-9_-]+)")

FIELD_LABELS = ["host", "hosts", "location", "venue", "address", "date", "time", "date_time", "description", "notes", "contact", "organizer", "price", "capacity"]



# --- anti-detection init script ---
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
  { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
];
Object.defineProperty(navigator, 'mimeTypes', { get: () => mimeTypes });

if (!window.chrome) {
  window.chrome = { runtime: {}, loadTimes: () => ({}) };
}
"""

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def robots_allow(url: str) -> tuple[bool, float | None]:
    from urllib.parse import urlparse
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        req = urllib.request.Request(base, headers={"User-Agent": random.choice(UA_POOL)})
        with urllib.request.urlopen(req, timeout=8) as r:
            text = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log(f"robots.txt not fetched ({e.__class__.__name__}); allowing")
        return True, None
    crawl_delay = None
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("crawl-delay:"):
            try:
                crawl_delay = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    if not crawl_delay:
        return True, None
    log(f"robots.txt Crawl-delay={crawl_delay}s — will honor as minimum gap")
    return True, crawl_delay


def human_dwell(page) -> None:
    steps = random.randint(3, 6)
    for _ in range(steps):
        page.mouse.wheel(0, random.randint(400, 1200))
        time.sleep(random.uniform(0.5, 1.6))
    if random.random() < 0.4:
        page.evaluate("window.scrollTo(0,0)")
        time.sleep(random.uniform(0.4, 1.0))


def extract(page) -> dict:
    title = page.title()
    body = page.evaluate(
        "() => {"
        "  const root = document.body;"
        "  if (!root) return '';"
        "  const block = document.querySelector('main') || root;"
        "  return block.innerText || '';"
        "}"
    )
    fields: dict[str, str] = {}
    for m in FIELD_RE.finditer(body):
        key = m.group(1).lower().replace(" ", "_").replace("&", "_")
        if key not in fields:
            fields[key] = m.group(2)[:500]
    return {"title": title, "body": body, "fields": fields}


def scrape_once(url: str, ctx) -> dict:
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        human_dwell(page)
        data = extract(page)
        data["url"] = url
        data["scraped_at"] = datetime.now().isoformat()
        return data
    finally:
        page.close()


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2), encoding="utf-8")


def draw_budget(state: dict) -> tuple[str, int]:
    today = date.today().isoformat()
    if state.get("date") == today:
        return today, int(state.get("budget", MAX_DAILY))
    budget = random.randint(MIN_DAILY, MAX_DAILY)
    state.update({"date": today, "budget": budget, "used_today": 0})
    save_state(state)
    return today, budget


def wait_for_window(start: dtime, end: dtime) -> None:
    while True:
        now = datetime.now().time()
        if start <= now < end:
            return
        if now < start:
            delta = (datetime.combine(date.today(), start) - datetime.now())
            secs = max(0, int(delta.total_seconds()))
            log(f"outside window — sleeping until {start.strftime('%H:%M')} (~{secs // 60} min)")
            time.sleep(min(secs, 600))
        else:
            log(f"past window end ({end}); exiting. Re-run tomorrow.")
            sys.exit(0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap today's visits (over budget)")
    ap.add_argument("--fresh", action="store_true", help="ignore saved files")
    ap.add_argument("--url", type=str, default=None, help="scrape a single URL (bypasses CSV)")
    args = ap.parse_args()

    df = None
    if not args.url:
        if not CSV_IN.exists():
            log(f"missing {CSV_IN}")
            sys.exit(1)
        df = pd.read_csv(CSV_IN)
        if "event_link" not in df.columns:
            log(f"no 'event_link' column in {CSV_IN}; columns={list(df.columns)}")
            sys.exit(1)

    if args.url:
        m = TOKEN_RE.search(args.url)
        if not m:
            log(f"could not extract token from: {args.url}")
            sys.exit(1)
        unique = [(m.group(1), args.url)]
        log(f"single URL mode: token={unique[0][0]}")
    else:
        links = df["event_link"].dropna().astype(str).tolist()
        seen, unique = set(), []
        for u in links:
            m = TOKEN_RE.search(u)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                unique.append((m.group(1), u))
        log(f"unique tokens: {len(unique)}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    _ok, min_gap = robots_allow(unique[0][1] if unique else "https://partiful.com/e/x")

    state = load_state()
    today, budget = draw_budget(state)
    cap = min(budget, args.limit) if args.limit else budget
    log(f"today={today} budget={budget} cap={cap} used={state.get('used_today', 0)}")

    todo = [
        (tok, url) for tok, url in unique
        if args.fresh or not (OUT_DIR / f"{tok}.json").exists()
    ]
    log(f"remaining to scrape: {len(todo)}")
    if not todo:
        log("nothing to do.")
        return

    wait_for_window(parse_hhmm(WINDOW_START), parse_hhmm(WINDOW_END))

    base_gap_lo = max((SPREAD_HOURS * 3600) / max(cap, 1) * 0.7, 4 * 60)
    base_gap_hi = max((SPREAD_HOURS * 3600) / max(cap, 1) * 1.3, 7 * 60)
    if min_gap:
        base_gap_lo = max(base_gap_lo, min_gap)
        base_gap_hi = max(base_gap_hi, min_gap)

    summary_path = SUMMARY
    summary_exists = summary_path.exists()

    def summary_row(d: dict) -> dict:
        f = d.get("fields", {})
        def pick(*keys):
            for k in keys:
                if k in f and f[k]:
                    return f[k]
            return ""
        return {
            "token": d.get("token"),
            "event_link": d.get("url"),
            "page_title": d.get("title"),
            "host": pick("host", "hosts", "organizer"),
            "location": pick("location", "venue", "address"),
            "date": pick("date"),
            "time": pick("time", "date_time"),
            "description": pick("description", "notes"),
            "status": d.get("status"),
            "scraped_at": d.get("scraped_at"),
        }

    with sync_playwright() as pw:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=HEADLESS,
            user_agent=random.choice(UA_POOL),
            viewport={"width": random.randint(1300, 1520), "height": random.randint(850, 1000)},
            locale="en-US",
            timezone_id="America/Los_Angeles",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(INIT_SCRIPT)

        used = int(state.get("used_today", 0))
        for i, (tok, url) in enumerate(todo, 1):
            if used >= cap:
                log(f"daily budget exhausted ({used}/{cap}); stopping. Re-run tomorrow.")
                break
            log(f"({i}/{len(todo)}) {tok}  [used {used}/{cap}]")
            for attempt in range(1, 4):
                try:
                    d = scrape_once(url, ctx)
                    d["token"] = tok
                    (OUT_DIR / f"{tok}.json").write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
                    with summary_path.open("a" if summary_exists else "w", newline="", encoding="utf-8") as fh:
                        w = csv.DictWriter(fh, fieldnames=list(summary_row(d).keys()))
                        if not summary_exists:
                            w.writeheader()
                        w.writerow(summary_row(d))
                    used += 1
                    state["used_today"] = used
                    save_state(state)
                    break
                except Exception as e:
                    log(f"  attempt {attempt}/3 failed: {e.__class__.__name__}: {e}")
                    if attempt == 3:
                        log(f"  skipping {tok} for now (re-runnable)")
                    else:
                        time.sleep(random.uniform(3, 7) * attempt)
            if used < cap:
                gap = random.uniform(base_gap_lo, base_gap_hi)
                if random.random() < 0.10:
                    gap += random.uniform(20, 45)
                log(f"  waiting {gap:.0f}s before next")
                time.sleep(gap)

        ctx.close()

    log("done for this run.")


if __name__ == "__main__":
    main()
