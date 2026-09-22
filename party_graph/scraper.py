"""Single-event scraping: navigate, dwell, extract, return data dict."""
from __future__ import annotations

from datetime import datetime

from party_graph.browser import human_dwell
from party_graph.extract import extract
from party_graph.utils import log


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
