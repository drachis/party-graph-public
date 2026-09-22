"""Partiful event-link scraper — CLI entry point.

Usage:
    python scrape_events.py                 # run today's budget (evening window)
    python scrape_events.py --limit 2       # cap today's visits at 2
    python scrape_events.py --fresh         # ignore saved files, re-scrape
    python scrape_events.py --url <URL>     # scrape a single URL
    python scrape_events.py --now           # skip the evening window
    python scrape_events.py --login         # open browser, log in, paste URLs interactively
    python scrape_events.py --gather-dates  # date-only pass, newest RSVP first,
                                             # no guest-list interaction at all
"""
from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Partiful event scraper with daily budget and evening window.",
    )
    ap.add_argument("--limit", type=int, default=None,
                    help="cap today's visits (overrides budget ceiling; "
                         "also caps --gather-dates)")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore already-saved files, re-scrape")
    ap.add_argument("--url", type=str, default=None,
                    help="scrape a single URL (bypasses CSV)")
    ap.add_argument("--now", action="store_true",
                    help="skip the evening window, run immediately")
    ap.add_argument("--login", action="store_true",
                    help="open browser for manual login, then read URLs from stdin")
    ap.add_argument("--gather-dates", action="store_true",
                    help="lightweight pass: visit each CSV event (newest RSVP "
                         "first), dwell ~20-30s, record only its real date "
                         "into scraped_events/event_dates.json -- never "
                         "touches the guest list, ignores the daily budget "
                         "and evening window")
    args = ap.parse_args()

    # Lazy import so --help works without playwright installed
    from party_graph.pipeline import run, run_gather_dates
    if args.gather_dates:
        run_gather_dates(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
