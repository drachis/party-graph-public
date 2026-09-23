# party-graph

A Partiful RSVP scraper and a self-hosted event calendar/heatmap built from
your own RSVP history.

**This repo contains no personal data.** It's the tool only -- scraper +
calendar generator. To use it, you supply your own Partiful data export and
run it locally against that. See "Your data" below.

## What's here

- `party_graph/` -- the scraper package (browser automation, extraction,
  pacing/state, output).
- `scrape_events.py` -- CLI entry point for scraping.
- `event_calendar.py` -- builds a local, self-hosted calendar/heatmap from
  whatever's been scraped so far.

## Setup

```
setup.bat            # creates .venv, installs deps + Playwright's Chromium
```
(or manually: `pip install -r requirements.txt -r requirements-scrape.txt`,
then `playwright install chromium`)

## Your data

Partiful lets you request an export of your own account data (RSVP history,
hosted events, etc.) -- that export is what this tool reads. Two files
matter:

- `data-request/rsvp_details.csv` -- required. Every event you've RSVP'd to.
- `data-request/host_details.csv` -- optional. Events you've hosted.

Drop those into `data-request/` (the placeholder dirs are already there) and
you're set. Everything the tool writes -- scraped event JSON, the date cache,
the generated calendar -- lands in `scraped_events/`. Both directories are
gitignored here on purpose: **never commit real data into this repo.** If you
want your data version-controlled (recommended, since gathering it is slow
and rate-limited -- see below), keep it in a *separate, private* repo instead.
A convenient way to combine the two locally: check out this repo as a git
submodule inside your private data repo (e.g. at `tool/`), then run
everything from the private repo's root:

```
git submodule add https://github.com/<you>/<your-private-repo>.git   # once
cd your-private-repo
git submodule add https://github.com/drachis/party-graph-public.git tool
python tool/scrape_events.py --gather-dates
python tool/event_calendar.py
```

All of this tool's file paths (`data-request/`, `scraped_events/`) are
resolved relative to your current directory, not to where the scripts live --
that's what makes running them as `tool/scrape_events.py` from a sibling data
repo work.

## Usage

```
python scrape_events.py                 # full scrape: today's budget, evening window, guest lists included
python scrape_events.py --gather-dates  # lightweight: date/title only, no guest-list interaction, no budget/window
python scrape_events.py --url <URL>     # scrape a single event
python scrape_events.py --now           # skip the evening-window wait
python scrape_events.py --login         # open a browser, log in manually, then paste URLs interactively
python scrape_events.py --fresh         # ignore already-saved files, re-scrape everything
```

Both scrape modes are deliberately slow and paced to look like a human
browsing (random delays, occasional long pauses, a persistent browser
profile) rather than a script hammering the site -- expect a full pass over a
few hundred events to take multiple runs across sessions. `--gather-dates` is
the cheap, resumable way to backfill dates for your whole history without
touching guest lists at all; it walks your RSVPs newest-first and stops
itself if anything looks like a bot-detection page.

```
python event_calendar.py               # writes calendar_data.json + event_calendar.html, serves + opens it
python event_calendar.py --data-only   # just refresh calendar_data.json (fast, no Playwright) -- reload your open tab to see it
python event_calendar.py --no-serve    # write the files but don't start a server / open a browser
```

The calendar is a static HTML shell that fetches `calendar_data.json` and
renders everything (month grid, heatmap, click-to-expand day detail) client
side. That's why it needs to be served over `http://` rather than opened
directly as a `file://` URL -- browsers block `fetch()` of local files.
`event_calendar.py` starts a small local server for you and opens it; leave
that running and re-run `--data-only` whenever you've gathered more data, then
just reload the page.

## Design notes

- Two independent date layers: the day you *RSVP'd* (always known, from the
  CSV) and the actual *event date* (known once scraped or gathered). An
  event you RSVP'd to today for a party three months out shows up on both.
- The week heatmap only counts confirmed, non-declined events -- RSVPing to
  a backlog never inflates it.
- All page extraction is text-based (`innerText` + regexes), not DOM
  selectors, since Partiful is a React SPA whose markup isn't a stable
  target.
