"""Output: save event JSON, append summary CSV row."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from party_graph.config import EVENT_DATES, OUT_DIR, SUMMARY


def event_path(token: str) -> Path:
    return OUT_DIR / f"{token}.json"


def load_event_dates() -> dict:
    """Read the gather-dates cache: {token: {title, url, raw_date, raw_time,
    scraped_at}}. Missing/corrupt file just means nothing gathered yet."""
    if EVENT_DATES.exists():
        try:
            return json.loads(EVENT_DATES.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_event_dates(data: dict) -> None:
    EVENT_DATES.parent.mkdir(parents=True, exist_ok=True)
    EVENT_DATES.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def save_event(data: dict) -> Path:
    """Write the full event dict to scraped_events/<token>.json."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = event_path(data["token"])
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def summary_row(d: dict) -> dict:
    """Flatten an event dict into a single-row CSV dict."""
    f = d.get("fields", {})

    def pick(*keys: str) -> str:
        for k in keys:
            if k in f and f[k]:
                return f[k]
        return ""

    return {
        "token": d.get("token", ""),
        "event_link": d.get("url", ""),
        "title": d.get("title", ""),
        "host": pick("host"),
        "venue": pick("venue"),
        "address": pick("address"),
        "date": pick("date"),
        "time": pick("time"),
        "description": pick("description"),
        "rsvp_going": pick("rsvp_going"),
        "rsvp_interested": pick("rsvp_interested"),
        "rsvp_maybe": pick("rsvp_maybe"),
        "scraped_at": d.get("scraped_at", ""),
    }


def append_summary(row: dict) -> None:
    """Append one row to the summary CSV, creating it with headers if needed."""
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    exists = SUMMARY.exists()
    with SUMMARY.open("a" if exists else "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row.keys()))
        if not exists:
            w.writeheader()
        w.writerow(row)
