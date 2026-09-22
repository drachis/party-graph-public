"""Extraction: page → {title, body, fields, guests}.

All extraction is text-based (page body innerText) to avoid brittle
DOM selectors on a React SPA.
"""
from __future__ import annotations

import re

from party_graph.config import (
    ADDR_RE,
    DATE_RE,
    GUEST_MARKER,
    GUEST_ROW_RE,
    HOST_MARKER,
    RSPV_STATS_RE,
    TIME_RE,
)

_BOILERPLATE = {
    "Get the app", "Login", "Log in", "Remind me later", "Follow",
    "View all", "Learn More", "RSVP", "Interested", "Explore events",
    "Create a free event", "English", "Help", "Privacy Choices",
    "Blog", "Careers", "About", "Home", "Explore", "Create",
    "Send a card", "Restricted Access", "Trending This Week",
    "See all in \U0001f5fd\ufe0f Trending in NYC", "\U0001f44d",
    "Already RSVP'd? Sign in",
}

_TZ_RE = re.compile(r"^(ET|PT|CT|MT|PST|PDT|EST|EDT|CST|CDT|MST|MDT|GMT|UTC)$")


def parse_guests_from_text(body: str) -> dict:
    """Extract guest names + status + date from the page body text."""
    guests: dict[str, list[dict]] = {
        "going": [], "maybe": [], "cant_go": [], "interested": [],
    }
    status_map = {
        "Going": "going", "Went": "going",
        "Maybe": "maybe",
        "Can't Go": "cant_go",
        "Interested": "interested",
    }
    for m in GUEST_ROW_RE.finditer(body):
        name = m.group(1).strip()
        status = m.group(2)
        gd = m.group(3)
        key = status_map.get(status, "going")
        guests[key].append({"name": name, "status": status, "date": gd})
    return guests


def extract_fields(body: str) -> dict[str, str]:
    """Walk the body text line-by-line, tracking section context.

    Returns a dict with keys: host, venue, address, date, time,
    description, rsvp_going, rsvp_interested, rsvp_maybe.
    """
    fields: dict[str, str] = {}
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]

    # RSVP stats
    stats_match = RSPV_STATS_RE.search(body)
    if stats_match:
        fields["rsvp_going"] = stats_match.group(1)
        fields["rsvp_interested"] = stats_match.group(2)
        fields["rsvp_maybe"] = stats_match.group(3)

    in_host = False
    in_venue = False
    description_parts: list[str] = []

    for line in lines:
        if line in _BOILERPLATE or line.startswith("We use cookies"):
            if line.startswith("We use cookies"):
                break
            continue

        # Host section
        if HOST_MARKER.match(line):
            in_host = True
            continue
        if in_host:
            if "upcoming events" not in line and not line.isdigit():
                fields.setdefault("host", line)
                in_host = False
                in_venue = True
            continue

        # Venue / address (follows host)
        if in_venue:
            if re.search(r"\d+\s+(upcoming events|followers|events)", line, re.I):
                continue
            if ADDR_RE.search(line) or re.search(r",\s*[A-Z]{2}\b", line):
                fields.setdefault("address", line)
            else:
                fields.setdefault("venue", line)
            in_venue = False
            continue

        # Guest list marker ends the metadata zone
        if GUEST_MARKER.match(line):
            break

        # Date
        if DATE_RE.match(line):
            fields.setdefault("date", line)
            continue

        # Time
        if TIME_RE.match(line):
            fields.setdefault("time", line)
            continue

        # Timezone hints
        if _TZ_RE.match(line):
            continue

        # Address heuristic (street suffix or US state code)
        if re.search(
            r"\b(St\.|Ave\.|Dr\.|Blvd\.|Rd\.|Ln\.|Way|Pl\.|Pkwy\.|NY|CA|TX|FL|IL|WA|MA|PA|OH|GA|NC|MI|AZ|NV|OR|CO|VA|TN|IN|MO|MD|WI|MN|AL|SC|LA|KY|AR|MS|OK|IA|KS|NE|NV|UT|ID|NM|WV|HI|NH|ME|RI|DE|MD|VT|WY|MT|ND|SD|AK|DC|PR)\b",
            line,
        ):
            fields.setdefault("address", line)
            continue

        # After date/time: accumulate description
        if "date" in fields or "time" in fields:
            if line and not line.startswith("+") and len(line) > 2:
                description_parts.append(line)

    if description_parts:
        fields.setdefault("description", " ".join(description_parts))

    return fields


def extract(page) -> dict:
    """Extract all data from a loaded Playwright page.

    Returns {title, body, fields, guests}.
    """
    title = page.title()
    body = page.evaluate(
        "() => {"
        "  const block = document.querySelector('main') || document.body;"
        "  return block ? block.innerText : '';"
        "}"
    )

    if title.endswith(" | Partiful"):
        title = title[:-11].strip()

    fields = extract_fields(body)
    guests = parse_guests_from_text(body)

    return {"title": title, "body": body, "fields": fields, "guests": guests}
