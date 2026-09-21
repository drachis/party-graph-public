"""
One-page heatmapped event calendar.

Reads data-request/rsvp_details.csv with columns:
    title, event_link, status, rsvp_date, plus_one_count

Renders each ISO week (numbered gutter, heat colored) x each weekday, with a
marker per day sized+colored by event count. Day and week counts share a single
green (0) -> red (global max) scale so they're directly comparable.

Opens the result in the default browser and also saves event_calendar.html.
"""

import sys
import webbrowser
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

CSV = Path(__file__).parent / "data-request" / "rsvp_details.csv"
OUT = Path(__file__).parent / "event_calendar.html"


def parse_date(val):
    """Parse a date value into a datetime.date, trying several formats.

    Handles plain dates, ISO timestamps, and strings like
    'Sep 08, 2023 03:51:32 PM UTC' (trailing timezone word).
    """
    if val is None:
        return None
    if isinstance(val, (pd.Timestamp, datetime)):
        return val.date()
    if isinstance(val, date):
        return val

    s = str(val).strip()
    if not s:
        return None

    # Strip a trailing timezone token like 'UTC', 'GMT', 'PDT', 'EST'.
    s_no_tz = s
    for suffix in (" UTC", " GMT", " ET", " PT", " CT", " MT",
                   " PDT", " PST", " EDT", " EST", " CDT", " CST"):
        if s_no_tz.endswith(suffix):
            s_no_tz = s_no_tz[: -len(suffix)]
            break

    for base in (s, s_no_tz):
        for fmt in (
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%m/%d/%Y",
            "%m/%d/%y",
            "%d-%b-%Y",
            "%b %d, %Y",
            "%b %d, %Y %I:%M:%S %p",
            "%b %d, %Y %H:%M:%S",
        ):
            try:
                return datetime.strptime(base, fmt).date()
            except ValueError:
                continue

    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None


def load(path):
    """Return (df, day_counts, week_counts, parse_failures)."""
    df = pd.read_csv(path)
    expected = ["title", "event_link", "status", "rsvp_date", "plus_one_count"]
    missing = [c for c in expected if c not in df.columns]
    if missing:
        sys.exit(f"Missing expected columns in {path}: {missing}\n"
                 f"Columns found: {list(df.columns)}")

    df["_date"] = df["rsvp_date"].map(parse_date)
    parse_failures = int(df["_date"].isna().sum())
    df = df[df["_date"].notna()].copy()
    if len(df) == 0:
        sys.exit("No parseable rsvp_date values found in the CSV.")

    day_counts = df.groupby("_date").size()

    def iso_label(d):
        return (d.isocalendar().year, d.isocalendar().week)

    week = df.copy()
    week["_week"] = week["_date"].map(iso_label)
    week_counts = week.groupby("_week").size()

    return df, day_counts, week_counts, parse_failures


def color_for(value, max_count):
    """rgb() on a green(0) -> yellow -> red(max) scale."""
    if max_count <= 0:
        return "rgb(144,238,144)"
    t = max(0.0, min(1.0, value / max_count))
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


def main():
    df, day_counts, week_counts, parse_failures = load(CSV)
    max_count = int(max(max(day_counts), max(week_counts)))

    months = sorted({d.strftime("%Y-%m") for d in df["_date"]})
    rows = len(months)
    fig = make_subplots(rows=rows, cols=1, vertical_spacing=0.12)

    weekday_ticks = [1, 2, 3, 4, 5, 6, 7]
    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    for r, month in enumerate(months, start=1):
        month_days = [d for d in day_counts.index if d.strftime("%Y-%m") == month]
        if not month_days:
            continue
        weeks_in_month = sorted({(d.isocalendar().year, d.isocalendar().week)
                                 for d in month_days})

        xvals, yvals, colors, sizes, textvals = [], [], [], [], []
        for d in month_days:
            iso = d.isocalendar()
            cnt = int(day_counts[d])
            xvals.append(iso.week)
            yvals.append(iso.weekday)
            titles = df[df["_date"] == d]["title"].tolist()
            textvals.append(f"{d.isoformat()}  ({cnt} event(s))\n" +
                            "\n".join(titles[:10]))
            colors.append(color_for(cnt, max_count))
            sizes.append(12 + 8 * (cnt / max_count))

        fig.add_trace(
            go.Scatter(
                x=xvals, y=yvals,
                mode="markers",
                marker=dict(size=sizes, color=colors,
                            line=dict(width=1.5, color="white")),
                text=textvals, hoverinfo="text",
                showlegend=False,
            ),
            row=r, col=1,
        )

        for (y, w) in weeks_in_month:
            cnt = int(week_counts.get((y, w), 0))
            fig.add_annotation(
                x=w, y=4,
                text=f"W{w:02d} ({cnt})",
                xref=f"x {r}", yref=f"y {r}",
                xanchor="right", xshift=-46,
                showarrow=False,
                font=dict(size=10),
                bgcolor=color_for(cnt, max_count),
                bordercolor="black", borderwidth=1, borderpad=3,
            )

    fig.update_yaxes(tickvals=weekday_ticks, ticktext=weekday_names,
                     range=[0.4, 7.6], title_text=None)
    fig.update_xaxes(title_text="ISO Week Number")

    for r, month in enumerate(months, start=1):
        fig.add_annotation(
            x=0, y=7.4, text=month,
            xref=f"x {r}", yref=f"y {r}",
            xanchor="left", showarrow=False,
            font=dict(size=14, color="#222"),
        )

    fig.update_layout(
        title=dict(text=f"Events by day & week — {len(df)} events · max {max_count} per unit",
                   x=0.05),
        showlegend=False,
        height=260 * rows + 150,
        width=1000,
        margin=dict(l=120, r=40, t=80, b=60),
    )

    fig.write_html(str(OUT), include_plotlyjs="cdn")
    webbrowser.open(OUT.resolve().as_uri())
    print(f"Wrote {OUT} and opened in browser.")
    print(f"{len(df)} events across {len(months)} month(s); max_count={max_count}.")
    if parse_failures:
        print(f"WARNING: {parse_failures} row(s) had unparseable rsvp_date and were skipped.")


if __name__ == "__main__":
    main()
