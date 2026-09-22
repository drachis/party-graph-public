"""State management: budget ledger, window wait, robots.txt check."""
from __future__ import annotations

import json
import random
import sys
import time
import urllib.request
from datetime import date, datetime
from datetime import time as dtime

from party_graph.config import (
    MAX_DAILY,
    MIN_DAILY,
    STATE,
    UA_POOL,
)
from party_graph.utils import log


# --- Budget ledger ---

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
    """Return (today_iso, budget). Resets the ledger on a new calendar day."""
    today = date.today().isoformat()
    if state.get("date") == today:
        return today, int(state.get("budget", MAX_DAILY))
    budget = random.randint(MIN_DAILY, MAX_DAILY)
    state.update({"date": today, "budget": budget, "used_today": 0})
    save_state(state)
    return today, budget


# --- Evening window ---

def wait_for_window(start: dtime, end: dtime) -> None:
    """Block until local time is within [start, end). Exit if past end."""
    while True:
        now = datetime.now().time()
        if start <= now < end:
            return
        if now < start:
            delta = datetime.combine(date.today(), start) - datetime.now()
            secs = max(0, int(delta.total_seconds()))
            log(f"outside window — sleeping until {start.strftime('%H:%M')} (~{secs // 60} min)")
            time.sleep(min(secs, 600))
        else:
            log(f"past window end ({end}); exiting. Re-run tomorrow.")
            sys.exit(0)


# --- Robots.txt ---

def robots_allow(url: str) -> tuple[bool, float | None]:
    """Check robots.txt for the URL's origin. Returns (allowed, crawl_delay)."""
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
