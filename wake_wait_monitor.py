#!/usr/bin/env python3
"""Monitor early voting wait times in Wake County, NC.

WakeVotesEarly.com redirects to the Wake County Board of Elections page,
which links to a public Airtable "Wait Time Tracker" shared view. This
script loads that shared view the same way the browser does (the embed
page contains a pre-signed readSharedViewData URL), parses the rows, and
prints the current estimated wait at each site. In watch mode it polls
periodically, highlights changes, and can append every observation to a
CSV file for later analysis.

Standard library only. Examples:
    python3 wake_wait_monitor.py --once
    python3 wake_wait_monitor.py --interval 300 --csv waits.csv
    python3 wake_wait_monitor.py --once --json
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

APP_ID = "appAL6KsUEvshj4br"
SHARE_ID = "shrQkIIFla4bjdGzh"
EMBED_URL = f"https://airtable.com/embed/{APP_ID}/{SHARE_ID}"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# Column names in the Airtable view (matched by name so ID changes don't break us).
COL_NAME = "Name"
COL_WAIT = "Estimated Wait Time"
COL_ADDRESS = "Early Voting Site Address"
COL_UPDATED = "Wait Time Estimate Last Updated At"
COL_DIRECTIONS = "Travel Directions (Google Maps)"

COLORS = {
    "greenDark": "\033[32m",
    "cyanDark": "\033[36m",
    "yellowDark": "\033[33m",
    "orangeDark": "\033[33m",
    "redDark": "\033[31m",
    "purpleDark": "\033[35m",
}
BOLD = "\033[1m"
RESET = "\033[0m"


def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def fetch_table():
    """Return the Airtable table dict (columns + rows) for the shared view."""
    html = http_get(EMBED_URL)
    m = re.search(r'urlWithParams:\s*"([^"]+)"', html)
    if not m:
        raise RuntimeError(
            "Could not find data URL in Airtable embed page (layout changed?)"
        )
    # The URL is embedded in a JS string literal with / escapes.
    path = json.loads(f'"{m.group(1)}"')
    body = http_get(
        "https://airtable.com" + path,
        headers={
            "x-airtable-application-id": APP_ID,
            "x-requested-with": "XMLHttpRequest",
            "x-time-zone": "America/New_York",
            "x-user-locale": "en",
        },
    )
    data = json.loads(body)
    return data["data"]["table"]


def city_of(address):
    """'125 Avery St, Garner, NC 27529' -> 'Garner'."""
    parts = [p.strip() for p in address.split(",")]
    return parts[-2] if len(parts) >= 3 else ""


def parse_sites(table):
    """Convert raw Airtable rows into a list of site dicts sorted by city, then wait (worst first)."""
    cols = {c["name"]: c for c in table["columns"]}
    wait_col = cols[COL_WAIT]
    choices = wait_col["typeOptions"]["choices"]
    order = {cid: i for i, cid in enumerate(wait_col["typeOptions"]["choiceOrder"])}

    def cell(row, name):
        col = cols.get(name)
        return row["cellValuesByColumnId"].get(col["id"]) if col else None

    sites = []
    for row in table["rows"]:
        choice_id = cell(row, COL_WAIT)
        choice = choices.get(choice_id, {})
        directions = cell(row, COL_DIRECTIONS)
        updated = cell(row, COL_UPDATED)
        sites.append(
            {
                "name": cell(row, COL_NAME) or "",
                "wait": choice.get("name", "Unknown"),
                "wait_rank": order.get(choice_id, -1),
                "color": choice.get("color"),
                "address": cell(row, COL_ADDRESS) or "",
                "city": city_of(cell(row, COL_ADDRESS) or ""),
                "updated_utc": updated,
                "directions": directions.get("url")
                if isinstance(directions, dict)
                else None,
            }
        )
    sites.sort(key=lambda s: (s["city"], -s["wait_rank"], s["name"]))
    return sites


def updated_ago(iso_utc, now):
    """'2026-10-15T12:02:20.000Z' -> '12 min ago' (or '1 h 5 min ago', '2 days ago')."""
    if not iso_utc:
        return ""
    dt = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    mins = max(int((now - dt).total_seconds() // 60), 0)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 24 * 60:
        h, m = divmod(mins, 60)
        return f"{h} h {m} min ago" if m else f"{h} h ago"
    days = mins // (24 * 60)
    return f"{days} day{'s' if days > 1 else ''} ago"


def print_table(sites, previous, use_color):
    now = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    name_w = max(len(s["name"]) for s in sites)
    city_w = max(len("City"), *(len(s["city"]) for s in sites))
    wait_w = max(len(s["wait"]) for s in sites)
    print(
        f"\n{BOLD if use_color else ''}Wake County Early Voting Wait Times — {now}"
        f"{RESET if use_color else ''}"
    )
    now_utc = datetime.now(UTC)
    ages = {s["name"]: updated_ago(s["updated_utc"], now_utc) for s in sites}
    age_w = max(len("Updated"), *(len(a) for a in ages.values()))
    print(
        f"{'Site':<{name_w}}  {'City':<{city_w}}  {'Wait':<{wait_w}}  {'Updated':<{age_w}}"
    )
    print(f"{'-' * name_w}  {'-' * city_w}  {'-' * wait_w}  {'-' * age_w}")
    for s in sites:
        wait = f"{s['wait']:<{wait_w}}"
        if use_color and s["color"] in COLORS:
            wait = f"{COLORS[s['color']]}{wait}{RESET}"
        note = ""
        old = previous.get(s["name"])
        if old is not None and old != s["wait"]:
            note = f"  <- was {old}"
            if use_color:
                note = f"{BOLD}{note}{RESET}"
        print(
            f"{s['name']:<{name_w}}  {s['city']:<{city_w}}  {wait}  {ages[s['name']]:<{age_w}}{note}".rstrip()
        )


def append_csv(path, sites, polled_at):
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["polled_at", "site", "wait", "site_updated_at", "address"])
        for s in sites:
            w.writerow(
                [polled_at, s["name"], s["wait"], s["updated_utc"] or "", s["address"]]
            )


DEFAULT_LOG = Path(__file__).with_name("logs") / "monitor.log"


def log_line(path, msg):
    """Append a timestamped line to the log file (no-op if path is empty)."""
    if not path:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}] {msg}\n")


def describe_changes(sites, previous):
    """'Cary Senior Center 15-30 Minutes -> 30-60 Minutes; ...' for sites whose wait changed."""
    return "; ".join(
        f"{s['name']} {previous[s['name']]} -> {s['wait']}"
        for s in sites
        if s["name"] in previous and previous[s["name"]] != s["wait"]
    )


def main():
    p = argparse.ArgumentParser(
        description="Monitor Wake County, NC early voting wait times."
    )
    p.add_argument(
        "--interval", type=int, default=300, help="seconds between polls (default 300)"
    )
    p.add_argument("--once", action="store_true", help="fetch once and exit")
    p.add_argument(
        "--csv", metavar="FILE", help="append each poll's results to this CSV file"
    )
    p.add_argument(
        "--json", action="store_true", help="print results as JSON instead of a table"
    )
    p.add_argument(
        "--site", metavar="TEXT", help="only show sites whose name contains TEXT"
    )
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    p.add_argument(
        "--log",
        metavar="FILE",
        default=str(DEFAULT_LOG),
        help="log each poll, change and error here (default logs/monitor.log; "
        "'' to disable)",
    )
    args = p.parse_args()
    log_line(
        args.log,
        f"monitor started (pid {os.getpid()}, interval {args.interval}s, "
        f"csv {args.csv or 'none'})",
    )

    use_color = sys.stdout.isatty() and not args.no_color and not args.json
    previous = {}

    while True:
        try:
            sites = parse_sites(fetch_table())
            if args.site:
                sites = [s for s in sites if args.site.lower() in s["name"].lower()]
            polled_at = datetime.now(UTC).isoformat(timespec="seconds")
            if args.csv:
                append_csv(args.csv, sites, polled_at)
            if args.json:
                print(json.dumps({"polled_at": polled_at, "sites": sites}, indent=2))
            elif sites:
                print_table(sites, previous, use_color)
            else:
                print("No matching sites.")
            changes = describe_changes(sites, previous)
            log_line(
                args.log,
                f"poll ok: {len(sites)} sites"
                + (f"; changed: {changes}" if changes else "; no changes"),
            )
            previous = {s["name"]: s["wait"] for s in sites}
        except Exception as e:  # keep monitoring through transient network/API errors
            log_line(args.log, f"poll failed: {e}")
            if args.once:
                raise
            print(f"[{datetime.now():%H:%M:%S}] fetch failed: {e}", file=sys.stderr)

        if args.once:
            break
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            break
    log_line(args.log, "monitor stopped")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
