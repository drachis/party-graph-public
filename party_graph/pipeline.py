"""Pipeline: load CSV, enforce budget, pace visits, retry, save results."""
from __future__ import annotations

import argparse
import random
import sys
import time

import pandas as pd
from playwright.sync_api import sync_playwright

from party_graph.browser import launch_context, scrape_login_mode
from party_graph.config import (
    CSV_IN,
    OUT_DIR,
    SPREAD_HOURS,
    TOKEN_RE,
    WINDOW_END,
    WINDOW_START,
)
from party_graph.output import append_summary, event_path, save_event, summary_row
from party_graph.scraper import scrape_once
from party_graph.state import draw_budget, load_state, robots_allow, save_state, wait_for_window
from party_graph.utils import log, parse_hhmm


def load_tokens(fresh: bool = False) -> list[tuple[str, str]]:
    """Read event links from CSV, deduplicate, return [(token, url)]."""
    if not CSV_IN.exists():
        log(f"missing {CSV_IN}")
        sys.exit(1)
    df = pd.read_csv(CSV_IN)
    if "event_link" not in df.columns:
        log(f"no 'event_link' column in {CSV_IN}; columns={list(df.columns)}")
        sys.exit(1)
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for u in df["event_link"].dropna().astype(str):
        m = TOKEN_RE.search(u)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            unique.append((m.group(1), u))
    return unique


def load_single_url(url: str) -> list[tuple[str, str]]:
    """Parse a single URL into [(token, url)]."""
    m = TOKEN_RE.search(url)
    if not m:
        log(f"could not extract token from: {url}")
        sys.exit(1)
    return [(m.group(1), url)]


def filter_todo(unique: list[tuple[str, str]], fresh: bool) -> list[tuple[str, str]]:
    """Remove tokens that already have a saved JSON (unless --fresh)."""
    if fresh:
        return unique
    return [(t, u) for t, u in unique if not event_path(t).exists()]


def compute_gaps(cap: int, min_gap: float | None) -> tuple[float, float]:
    """Derive the inter-visit gap range from the daily spread budget."""
    lo = max((SPREAD_HOURS * 3600) / max(cap, 1) * 0.7, 4 * 60)
    hi = max((SPREAD_HOURS * 3600) / max(cap, 1) * 1.3, 7 * 60)
    if min_gap:
        lo = max(lo, min_gap)
        hi = max(hi, min_gap)
    return lo, hi


def run(args: argparse.Namespace) -> None:
    """Main pipeline entry point."""
    # --- Load targets ---
    if args.url:
        unique = load_single_url(args.url)
        log(f"single URL mode: token={unique[0][0]}")
    else:
        unique = load_tokens(fresh=args.fresh)
        log(f"unique tokens: {len(unique)}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- Robots check ---
    _ok, min_gap = robots_allow(unique[0][1] if unique else "https://partiful.com/e/x")

    # --- Budget ---
    state = load_state()
    _today, budget = draw_budget(state)
    cap = min(budget, args.limit) if args.limit else budget
    used = int(state.get("used_today", 0))
    log(f"budget={budget} cap={cap} used={used}")

    todo = filter_todo(unique, args.fresh)
    log(f"remaining to scrape: {len(todo)}")
    if not todo:
        log("nothing to do.")
        return

    # --- Evening window ---
    if not args.now:
        wait_for_window(parse_hhmm(WINDOW_START), parse_hhmm(WINDOW_END))
    else:
        log("--now specified, skipping window wait")

    gap_lo, gap_hi = compute_gaps(cap, min_gap)

    # --- Browser + scrape loop ---
    with sync_playwright() as pw:
        ctx = launch_context(pw)

        # Login mode: interactive, no budget
        if args.login:
            scrape_login_mode(ctx)
            ctx.close()
            return

        # Normal mode: budgeted loop
        for i, (tok, url) in enumerate(todo, 1):
            if used >= cap:
                log(f"daily budget exhausted ({used}/{cap}); stopping. Re-run tomorrow.")
                break

            log(f"({i}/{len(todo)}) {tok}  [used {used}/{cap}]")

            for attempt in range(1, 4):
                try:
                    d = scrape_once(url, ctx)
                    d["token"] = tok
                    save_event(d)
                    append_summary(summary_row(d))
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

            # Inter-visit gap (skip after last item)
            if i < len(todo) and used < cap:
                gap = random.uniform(gap_lo, gap_hi)
                if random.random() < 0.10:
                    gap += random.uniform(20, 45)
                log(f"  waiting {gap:.0f}s before next")
                time.sleep(gap)

        ctx.close()

    log("done for this run.")
