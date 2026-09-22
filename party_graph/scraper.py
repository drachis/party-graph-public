"""Single-event scraping: navigate, dwell, extract, return data dict."""
from __future__ import annotations

from datetime import datetime

from party_graph.browser import human_dwell, long_dwell
from party_graph.config import GATHER_DWELL_MAX, GATHER_DWELL_MIN
from party_graph.extract import extract, extract_fields
from party_graph.utils import log

# Phrases that mean "this is a challenge/block page", not a real Partiful
# event -- checked on both HTTP status and page text so gather-dates can
# ditch out the moment it looks like the site is flagging us as a bot.
_BLOCK_SIGNS = (
    "captcha", "are you a robot", "unusual traffic", "access denied",
    "just a moment", "attention required", "too many requests",
    "rate limit", "verify you are human", "automated queries",
    "checking your browser", "cloudflare",
)


class BotDetected(Exception):
    """Raised when a page looks like an anti-bot challenge/block rather
    than a real event page. The caller should stop the whole run, not
    just skip this one item and move on."""


def _looks_blocked(page, response) -> str | None:
    """Return a short reason if this looks like a block/challenge page."""
    if response is not None and response.status in (403, 429, 503):
        return f"HTTP {response.status}"
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    if any(sign in title for sign in _BLOCK_SIGNS):
        return f"page title looks like a block page: {title!r}"
    try:
        snippet = page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 500) : ''"
        ).lower()
    except Exception:
        snippet = ""
    if any(sign in snippet for sign in _BLOCK_SIGNS):
        return "page body looks like a block/challenge page"
    return None


def gather_date_once(url: str, ctx) -> dict:
    """Visit a page just long enough to read its date -- no guest-list
    interaction at all, not even the text-based guest parsing extract()
    does. Returns {title, url, raw_date, raw_time, scraped_at}; the caller
    adds 'token' and merges it into the event_dates.json cache.

    Raises BotDetected if the page looks like a challenge/block rather than
    a real event -- checked before AND after the dwell, so a run never
    lingers on (or keeps going past) a page that flags us as a bot.
    """
    page = ctx.new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        reason = _looks_blocked(page, response)
        if reason:
            raise BotDetected(reason)

        long_dwell(page, GATHER_DWELL_MIN, GATHER_DWELL_MAX)

        reason = _looks_blocked(page, None)
        if reason:
            raise BotDetected(reason)

        title = page.title()
        if title.endswith(" | Partiful"):
            title = title[: -len(" | Partiful")].strip()
        body = page.evaluate(
            "() => {"
            "  const block = document.querySelector('main') || document.body;"
            "  return block ? block.innerText : '';"
            "}"
        )
        fields = extract_fields(body)
        return {
            "title": title,
            "url": url,
            "raw_date": fields.get("date", ""),
            "raw_time": fields.get("time", ""),
            "scraped_at": datetime.now().isoformat(),
        }
    finally:
        page.close()


def scrape_once(url: str, ctx) -> dict:
    """Open a page in the context, let it settle, extract all data.

    Returns a dict with: title, body, fields, guests, url, scraped_at.
    The caller adds 'token' before saving.
    """
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


def scrape_login_mode(ctx) -> None:
    """Interactive login mode: open Partiful, let user log in,
    then read event URLs from stdin and scrape them one by one.
    Does not consume the daily budget."""
    from party_graph.config import OUT_DIR, TOKEN_RE
    import json

    page = ctx.new_page()
    page.goto("https://partiful.com", wait_until="domcontentloaded", timeout=60000)
    print("\n=== LOGIN MODE ===")
    print("Browser is open. Log in to Partiful if needed.")
    print("Then paste an event URL and press Enter to scrape it:")
    print("(blank line to exit)\n")

    while True:
        url = input("> ").strip()
        if not url:
            break
        m = TOKEN_RE.search(url)
        if not m:
            log(f"could not extract token from: {url}")
            continue
        tok = m.group(1)
        log(f"scraping {tok}...")

        # Navigate with dwell
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        human_dwell(page)

        data = extract(page)
        data["token"] = tok
        data["url"] = url
        data["scraped_at"] = datetime.now().isoformat()

        out_path = OUT_DIR / f"{tok}.json"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"saved -> {out_path}")

        f = data.get("fields", {})
        if f:
            log("fields: " + ", ".join(f"{k}={v[:40]}" for k, v in f.items()))
        g = data.get("guests", {})
        total_guests = sum(len(v) for v in g.values())
        if total_guests:
            log(f"guests: {total_guests} total "
                + " ".join(f"({k}:{len(v)})" for k, v in g.items() if v))
        else:
            log("WARNING: no guests captured — may be restricted")

    page.close()
    log("login session saved. Re-run without --login to use the saved session.")
