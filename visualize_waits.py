#!/usr/bin/env python3
"""Build an HTML report of Wake County early voting wait times from the CSV
written by wake_wait_monitor.py --csv, alongside early voting turnout from the
NC State Board of Elections.

The report is a single self-contained HTML file (no network needed to view it):
  * Right now       - every site's latest reported wait, grouped by town
  * Through the day - site x time heatmap for one day, rows grouped by town,
                      with that day's early votes per site
  * Average wait    - mean estimated wait across sites over the day
  * Turnout by day  - early votes cast per day (State Board data)
  * Update frequency - when each site updated its estimate, typical and longest
                      gaps between updates, and how stale estimates were
  * Typical by hour - town x half-hour heatmap averaged over every day collected
  * Site summary    - sortable table (also the accessible view of the data)
  * Data notes      - each day's polling window and any gaps when the monitor wasn't
                      running, plus notes given with --note

Wait labels are shown exactly as the tracker reports them. For averages each
range ("15-30 Minutes") counts as its midpoint and an open-ended label ("2+ Hours")
as its lower bound; for color each wait falls in one of five bands, the last
being "Over 60 min" so longer choices the county may add are still handled.
Labels that can't be read as a time are reported when the script runs.

Turnout comes from the State Board's statewide absentee file, which is
refreshed each morning with the previous day's early votes. It is downloaded
to ncsbe_cache/ and only fetched again when the State Board has a newer copy.

Standard library only. Examples:
    python3 visualize_waits.py
    python3 visualize_waits.py waits.csv -o report.html --open
    python3 visualize_waits.py waits.csv more_waits.csv --note "Monitor started late Thursday."
    python3 visualize_waits.py --no-turnout
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import webbrowser
import zipfile
from collections import Counter
from datetime import UTC, datetime
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
BANDS = ["No wait", "Under 15 min", "15–30 min", "30–60 min", "Over 60 min"]

TRACKER_URL = "https://airtable.com/appAL6KsUEvshj4br/shrQkIIFla4bjdGzh"
COUNTY_URL = "https://wakevotesearly.com"
NCSBE_URL = "https://www.ncsbe.gov/results-data/absentee-and-provisional-data"
ELECTION = "20261103"
ABSENTEE_URL = "https://s3.amazonaws.com/dl.ncsbe.gov/ENRS/{d}/absentee_{ymd}.zip"
COUNTY = "WAKE"
# State Board site_name -> tracker site name, for any pair the word match gets wrong or misses.
SITE_ALIASES = {}
# Words too generic to identify a site.
STOPWORDS = {
    "the",
    "and",
    "of",
    "at",
    "community",
    "center",
    "recreation",
    "rec",
    "park",
    "building",
    "annex",
    "town",
    "county",
    "nc",
    "site",
}


def wait_minutes(label):
    """Return (minutes, band_index) for a wait label, or (None, None) if it isn't a time.

    Handles "No Wait", "5-15 Minutes", "45 Minutes - 1 Hour", "1-2 Hours", and
    open-ended labels such as "2+ Hours" or "More than 90 minutes". A number with
    no unit takes the next unit in the label ("1-2 Hours" -> 60-120 minutes).
    """
    text = label.strip().lower()
    if "no wait" in text:
        return 0.0, 0
    tokens = re.findall(r"(\d+(?:\.\d+)?)\+?\s*(h(?:ou)?rs?\b|min(?:ute)?s?\b)?", text)
    if not tokens:
        return None, None
    values, pending = [], []
    for num, unit in tokens:
        pending.append(float(num))
        if unit:
            values += [n * (60 if unit.startswith("h") else 1) for n in pending]
            pending = []
    values += pending  # trailing numbers with no unit: assume minutes
    lo, hi = min(values), max(values)
    open_ended = bool(re.search(r"\+|more|over|longer|greater|at least", text))
    minutes, top = (lo, lo) if open_ended or lo == hi else ((lo + hi) / 2, hi)
    if open_ended and lo >= 60:
        top = lo + 1  # "60+" belongs in the over-60 band
    band = 1 if top <= 15 else 2 if top <= 30 else 3 if top <= 60 else 4
    return minutes, band


def city_of(address):
    """'125 Avery St, Garner, NC 27529' -> 'Garner'."""
    parts = [p.strip() for p in address.split(",")]
    return parts[-2] if len(parts) >= 3 else "Unknown"


def load_rows(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            polled = datetime.fromisoformat(r["polled_at"]).astimezone(TZ)
            minutes, band = wait_minutes(r["wait"])
            updated = r.get("site_updated_at") or ""
            if updated:
                updated = datetime.fromisoformat(updated).astimezone(TZ)
                updated = updated.strftime("%Y-%m-%dT%H:%M")
            rows.append(
                {
                    "t": polled.strftime("%Y-%m-%dT%H:%M"),
                    "site": r["site"],
                    "city": city_of(r.get("address", "")),
                    "addr": r.get("address", ""),
                    "wait": r["wait"],
                    "min": minutes,
                    "band": band,
                    "u": updated,
                }
            )
    return rows


def fetch_absentee(election, cache_dir):
    """Download the State Board absentee zip if it changed; return (path, last_modified) or (None, None)."""
    url = ABSENTEE_URL.format(
        d=f"{election[:4]}_{election[4:6]}_{election[6:]}", ymd=election
    )
    path = cache_dir / f"absentee_{election}.zip"
    headers = {"User-Agent": "Mozilla/5.0"}
    if path.exists():
        headers["If-Modified-Since"] = formatdate(path.stat().st_mtime, usegmt=True)
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=120
        ) as resp:
            cache_dir.mkdir(exist_ok=True)
            tmp = path.with_suffix(".part")
            with open(tmp, "wb") as f:
                while chunk := resp.read(1 << 20):
                    f.write(chunk)
            tmp.replace(path)
            modified = resp.headers.get("Last-Modified")
            if modified:
                ts = parsedate_to_datetime(modified).timestamp()
                os.utime(path, (ts, ts))
            print(f"downloaded {url}")
    except urllib.error.HTTPError as e:
        if e.code != 304:
            print(f"turnout: could not download {url} ({e})", file=sys.stderr)
    except OSError as e:
        print(f"turnout: could not download {url} ({e})", file=sys.stderr)
    if not path.exists():
        return None, None
    return path, datetime.fromtimestamp(path.stat().st_mtime, UTC).astimezone(TZ)


def load_turnout(zip_path):
    """Count accepted in-person early votes in COUNTY by (State Board site name, ISO date)."""
    counts = Counter()
    with zipfile.ZipFile(zip_path) as z:
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(name) as raw:
            for r in csv.DictReader(
                io.TextIOWrapper(raw, encoding="latin-1", newline="")
            ):
                if (
                    r["county_desc"] == COUNTY
                    and r["ballot_req_type"] == "EARLY VOTING"
                    and r["ballot_rtn_status"].startswith("ACCEPTED")
                    and r["ballot_rtn_dt"].strip()
                ):
                    d = (
                        datetime.strptime(r["ballot_rtn_dt"].strip(), "%m/%d/%Y")
                        .date()
                        .isoformat()
                    )
                    counts[r["site_name"].strip(), d] += 1
    return counts


def words(text):
    text = re.sub(
        r"^[A-Z0-9]{1,4} - ", "", text
    )  # State Board names start with a site code, "CA - ..."
    return {
        w
        for w in re.findall(r"[a-z0-9]+", text.lower())
        if w not in STOPWORDS and len(w) > 1
    }


def match_sites(board_names, tracker_cities):
    """Map State Board site names to tracker site names by words unique to one tracker site.

    A tracker site's words include its town, so "HS - HOLLY SPRINGS - HUNT COMMUNITY CENTER"
    finds "W.E. Hunt Recreation Center" in Holly Springs. Words shared by several tracker
    sites ("wake", "raleigh") don't count. Each tracker site is used at most once.
    """
    tw = {s: words(s) | words(c) for s, c in tracker_cities.items()}
    seen = Counter(w for ws in tw.values() for w in ws)
    unique = {s: {w for w in ws if seen[w] == 1} for s, ws in tw.items()}
    out = {b: SITE_ALIASES[b] for b in board_names if b in SITE_ALIASES}
    pairs = sorted(
        (
            (len(words(b) & unique[s]), b, s)
            for b in board_names
            if b not in out
            for s in tracker_cities
        ),
        reverse=True,
    )
    used = set(out.values())
    for score, b, s in pairs:
        if score and b not in out and s not in used:
            out[b] = s
            used.add(s)
    return out


def build_turnout(rows, election, cache_dir):
    path, modified = fetch_absentee(election, cache_dir)
    if not path:
        return None
    counts = load_turnout(path)
    tracker = {r["site"]: r["city"] for r in rows}
    boards = sorted({b for b, _ in counts})
    mapping = match_sites(boards, tracker)
    for b in boards:
        if b not in mapping:
            print(
                f"turnout: no tracker site matches '{b}' (add it to SITE_ALIASES if it should)",
                file=sys.stderr,
            )
    for b, s in sorted(mapping.items()):
        print(f"turnout: '{b}' -> '{s}'")
    by = Counter()
    for (b, d), n in counts.items():
        by[mapping.get(b, b), d] += n
    return {
        "rows": [
            {"site": s, "city": tracker.get(s), "d": d, "n": n}
            for (s, d), n in sorted(by.items())
        ],
        "updated": modified.strftime("%a %b %-d %-I:%M %p"),
    }


def main():
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "csv",
        nargs="*",
        default=[here / "waits.csv"],
        help="input CSV(s); several are combined (default waits.csv)",
    )
    p.add_argument(
        "-o",
        "--output",
        default=None,
        help="output HTML (default waits_report.html next to the CSV)",
    )
    p.add_argument(
        "--open", action="store_true", help="open the report in a browser when done"
    )
    p.add_argument(
        "--note",
        action="append",
        default=[],
        help="add a note to the report's Data notes (repeatable)",
    )
    p.add_argument(
        "--no-turnout", action="store_true", help="skip State Board turnout data"
    )
    p.add_argument(
        "--election",
        default=ELECTION,
        help=f"election date YYYYMMDD for turnout (default {ELECTION})",
    )
    args = p.parse_args()

    srcs = [Path(c) for c in args.csv]
    rows, seen = [], set()
    for src in srcs:
        for r in load_rows(src):  # drop exact duplicates if files overlap
            key = (r["t"], r["site"])
            if key not in seen:
                seen.add(key)
                rows.append(r)
    if not rows:
        sys.exit(f"no rows in {', '.join(map(str, srcs))}")
    odd = sorted({r["wait"] for r in rows if r["min"] is None})
    if odd:
        print(
            f"note: wait labels not read as a time (shown as 'Other'): {', '.join(odd)}",
            file=sys.stderr,
        )
    turnout = (
        None
        if args.no_turnout
        else build_turnout(rows, args.election, here / "ncsbe_cache")
    )
    addresses = {}
    for r in rows:  # keep one address per site rather than repeating it on every row
        addresses[r["site"]] = r.pop("addr") or addresses.get(r["site"], "")
    out = Path(args.output) if args.output else srcs[0].with_name("waits_report.html")
    data = {
        "rows": rows,
        "bands": BANDS,
        "turnout": turnout,
        "notes": args.note,
        "addresses": addresses,
        "links": {"tracker": TRACKER_URL, "county": COUNTY_URL, "ncsbe": NCSBE_URL},
        "sources": [s.name for s in srcs],
        "generated": datetime.now(TZ).strftime("%a %b %-d, %Y %-I:%M %p"),
    }
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    out.write_text(TEMPLATE.replace("/*__DATA__*/null", payload))
    days = len({r["t"][:10] for r in rows})
    sites = len({r["site"] for r in rows})
    votes = sum(r["n"] for r in turnout["rows"]) if turnout else 0
    print(
        f"wrote {out} ({len(rows)} observations, {sites} sites, {days} day(s); {votes} early votes)"
    )
    if args.open:
        webbrowser.open(out.resolve().as_uri())


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wake Early Voting Waits</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --none: #ecebe6;
  --b0: #86b6ef; --b1: #5598e7; --b2: #2a78d6; --b3: #1c5cab; --b4: #0d366b;
  --line: #2a78d6; --line-soft: #b7d3f6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --none: #262624;
    --b0: #184f95; --b1: #2a78d6; --b2: #5598e7; --b3: #86b6ef; --b4: #cde2fb;
    --line: #3987e5; --line-soft: #184f95;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19;
  --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --none: #262624;
  --b0: #184f95; --b1: #2a78d6; --b2: #5598e7; --b3: #86b6ef; --b4: #cde2fb;
  --line: #3987e5; --line-soft: #184f95;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 0 0 2px; }
.sub { color: var(--ink-2); margin: 0 0 12px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px;
  padding: 16px; margin-top: 16px; }
.filters { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-top: 16px; }
.filters label { color: var(--ink-2); font-size: 13px; display: flex; gap: 6px; align-items: center; }
select { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--axis);
  border-radius: 6px; padding: 4px 8px; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--ink-2); margin: 8px 0 4px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.sw { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
.scroll { overflow-x: auto; }
svg { display: block; }
svg text { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
svg .lbl { fill: var(--ink-2); font-size: 12px; }
svg .tick { fill: var(--line); }
svg .gap { fill: var(--grid); }
svg .num { fill: var(--ink-2); font-size: 12px; }
svg .city { fill: var(--ink); font-size: 12px; font-weight: 600; }
.kpis { display: flex; flex-wrap: wrap; gap: 24px; margin: 8px 0 4px; }
.kpi .v { font-size: 28px; font-weight: 600; }
.kpi .k { color: var(--ink-2); font-size: 12px; }
.now { columns: 300px; column-gap: 32px; }
.town { break-inside: avoid; padding-bottom: 6px; }
.now h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
  color: var(--ink-2); margin: 10px 0 2px; }
.site { display: flex; align-items: center; gap: 8px; padding: 3px 0; }
a { color: inherit; text-decoration-color: var(--axis); text-underline-offset: 2px; }
a:hover { text-decoration-color: currentColor; }
.links { margin: -4px 0 0; font-size: 13px; color: var(--ink-2); }
.links a { color: var(--line); font-weight: 600; text-decoration: none; margin-right: 16px; }
.links a:hover { text-decoration: underline; }
svg a:hover text { fill: var(--line); text-decoration: underline; }
.site .n { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.site .w { color: var(--ink-2); font-size: 12px; white-space: nowrap; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th { color: var(--ink-2); font-weight: 600; font-size: 12px; cursor: pointer; user-select: none;
  white-space: normal; vertical-align: bottom; }
td.num, th.num { text-align: right; }
#tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink);
  border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; font-size: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,.15); display: none; z-index: 10; max-width: 280px; }
#tip b { display: block; margin-bottom: 2px; }
#tip .m { color: var(--ink-2); }
.empty { color: var(--muted); padding: 16px 0; }
.notes ul { margin: 8px 0 0; padding-left: 20px; color: var(--ink-2); }
.notes li { margin: 4px 0; }
</style>
</head>
<body>
<main>
  <h1>Wake County early voting wait times</h1>
  <p class="sub" id="meta"></p>
  <p class="links" id="links"></p>

  <section class="card" aria-labelledby="h-now">
    <h2 id="h-now">Right now</h2>
    <p class="sub" id="now-sub"></p>
    <div class="now" id="now"></div>
  </section>

  <div class="filters">
    <label>Day <select id="f-day"></select></label>
    <label>Town <select id="f-city"></select></label>
  </div>

  <section class="card" aria-labelledby="h-heat">
    <h2 id="h-heat">Through the day, by site</h2>
    <p class="sub">Each cell is one poll of the county's tracker. Sites are grouped by town; hover a cell for details.</p>
    <div class="legend" id="legend"></div>
    <div class="scroll" id="heat"></div>
  </section>

  <section class="card" aria-labelledby="h-avg">
    <h2 id="h-avg">Average estimated wait</h2>
    <p class="sub" id="avg-sub"></p>
    <div class="kpis" id="kpis"></div>
    <div id="avg"></div>
  </section>

  <section class="card" aria-labelledby="h-turn" id="turn-card">
    <h2 id="h-turn">Early votes cast per day</h2>
    <p class="sub" id="turn-sub"></p>
    <div class="kpis" id="turn-kpis"></div>
    <div id="turn"></div>
  </section>

  <section class="card" aria-labelledby="h-upd">
    <h2 id="h-upd">How often sites update their estimate</h2>
    <p class="sub" id="upd-sub"></p>
    <div class="kpis" id="upd-kpis"></div>
    <div class="scroll" id="upd"></div>
  </section>

  <section class="card" aria-labelledby="h-tod">
    <h2 id="h-tod">Typical wait by time of day, by town</h2>
    <p class="sub" id="tod-sub"></p>
    <div class="scroll" id="tod"></div>
  </section>

  <section class="card" aria-labelledby="h-tbl">
    <h2 id="h-tbl">Site summary</h2>
    <p class="sub" id="tbl-sub"></p>
    <div class="scroll"><table id="tbl"></table></div>
  </section>
  <section class="card notes" aria-labelledby="h-notes">
    <h2 id="h-notes">Data notes</h2>
    <ul id="notes"></ul>
  </section>
</main>
<div id="tip" role="tooltip"></div>
<script>
const DATA = /*__DATA__*/null;
const ROWS = DATA.rows, BANDS = DATA.bands, TURN = DATA.turnout, LINKS = DATA.links;
// Directions to a site from its address (the tracker's own directions link isn't in the CSV).
const mapUrl = site => DATA.addresses[site]
  ? "https://www.google.com/maps/dir/?api=1&destination=" + encodeURIComponent(DATA.addresses[site]) : null;
const siteLink = (site, cls = "") => { const u = mapUrl(site);
  return u ? `<a class="${cls}" href="${u}" target="_blank" rel="noopener" title="${DATA.addresses[site]} (directions)">${site}</a>`
    : `<span class="${cls}" title="${site}">${site}</span>`; };
function svgSiteLabel(svg, x, y, site, label) {  // site name in an SVG chart, linked to directions
  const u = mapUrl(site), parent = u ? el("a", { href: u, target: "_blank", rel: "noopener" }, svg) : svg;
  const t = text(parent, x, y, label, { class: "lbl" });
  if (u) el("title", {}, t).textContent = `${DATA.addresses[site]} (directions)`;
}
// votes[site][day] from the State Board file; sites the tracker doesn't list keep their Board name.
const VOTES = {}, TURN_CITY = {};
(TURN ? TURN.rows : []).forEach(r => { (VOTES[r.site] = VOTES[r.site] || {})[r.d] = r.n; TURN_CITY[r.site] = r.city; });
const fmtN = n => n == null ? "—" : n.toLocaleString();
const NS = "http://www.w3.org/2000/svg";
const bandVar = b => b == null ? "var(--none)" : `var(--b${b})`;
const bandOf = m => m == null ? null : m === 0 ? 0 : m <= 15 ? 1 : m <= 30 ? 2 : m <= 60 ? 3 : 4;
const day = t => t.slice(0, 10);
const mins = t => +t.slice(11, 13) * 60 + +t.slice(14, 16);
const clock = m => { const h = Math.floor(m / 60), mm = m % 60, h12 = (h + 11) % 12 + 1;
  return `${h12}${mm ? ":" + String(mm).padStart(2, "0") : ""} ${h < 12 ? "AM" : "PM"}`; };
const fmtMin = m => m == null ? "—" : `${Math.round(m)} min`;
const fmtDay = d => new Date(d + "T12:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
// Typical spacing between polls; a longer gap means the monitor wasn't running.
const POLL_GAP = (() => { const ts = [...new Set(ROWS.map(r => r.t))].sort(), d = [];
  for (let i = 1; i < ts.length; i++) if (ts[i].slice(0, 10) === ts[i - 1].slice(0, 10)) d.push(mins(ts[i]) - mins(ts[i - 1]));
  d.sort((a, b) => a - b); return d.length ? d[d.length >> 1] || 5 : 5; })();
const isGap = (a, b) => b - a > POLL_GAP * 3;
const mean = a => a.length ? a.reduce((s, x) => s + x, 0) / a.length : null;
const el = (tag, attrs = {}, parent) => { const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
const text = (parent, x, y, s, attrs = {}) => { const t = el("text", { x, y, ...attrs }, parent); t.textContent = s; return t; };

// Town order: most sites first, then alphabetical; sites alphabetical within a town.
const siteCity = {};
ROWS.forEach(r => siteCity[r.site] = r.city);
const cityCount = {};
Object.values(siteCity).forEach(c => cityCount[c] = (cityCount[c] || 0) + 1);
const CITIES = Object.keys(cityCount).sort((a, b) => cityCount[b] - cityCount[a] || a.localeCompare(b));
const DAYS = [...new Set(ROWS.map(r => day(r.t)))].sort();

// Tooltip
const tip = document.getElementById("tip");
function showTip(ev, html) { tip.innerHTML = html; tip.style.display = "block"; moveTip(ev); }
function moveTip(ev) { const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + w > innerWidth - 8) x = ev.clientX - w - pad;
  if (y + h > innerHeight - 8) y = ev.clientY - h - pad;
  tip.style.left = x + "px"; tip.style.top = y + "px"; }
const hideTip = () => tip.style.display = "none";
function hover(node, html) { node.addEventListener("mouseenter", e => showTip(e, html()));
  node.addEventListener("mousemove", moveTip); node.addEventListener("mouseleave", hideTip); }

// Header + legend
const last = ROWS.reduce((a, r) => r.t > a ? r.t : a, "");
document.getElementById("meta").textContent =
  `${Object.keys(siteCity).length} sites in ${CITIES.length} towns · ${DAYS.length} day${DAYS.length > 1 ? "s" : ""} of data · ` +
  `last poll ${fmtDay(day(last))} ${clock(mins(last))} · report built ${DATA.generated}`;
document.getElementById("legend").innerHTML = BANDS.map((b, i) =>
  `<span><i class="sw" style="background:var(--b${i})"></i>${b}</span>`).join("") +
  `<span><i class="sw" style="background:var(--none);box-shadow:inset 0 0 0 1px var(--axis)"></i>No data or other</span>`;

document.getElementById("links").innerHTML =
  `<a href="${LINKS.tracker}" target="_blank" rel="noopener">Live wait tracker ↗</a>` +
  `<a href="${LINKS.county}" target="_blank" rel="noopener">Early voting sites &amp; hours ↗</a>` +
  `<span>Site names link to directions.</span>`;

// Right now: latest poll per site, grouped by town, worst first within the town.
(function renderNow() {
  const latest = {};
  ROWS.forEach(r => { if (!latest[r.site] || r.t > latest[r.site].t) latest[r.site] = r; });
  document.getElementById("now-sub").textContent = `As of ${fmtDay(day(last))} ${clock(mins(last))}. Towns ordered by number of sites.`;
  const box = document.getElementById("now");
  box.innerHTML = CITIES.map(c => {
    const sites = Object.values(latest).filter(r => r.city === c)
      .sort((a, b) => (b.min ?? -1) - (a.min ?? -1) || a.site.localeCompare(b.site));
    return `<div class="town"><h3>${c}</h3>` + sites.map(r =>
      `<div class="site"><i class="sw" style="background:${bandVar(r.band)}"></i>` +
      `${siteLink(r.site, "n")}<span class="w">${r.wait}</span></div>`).join("") + `</div>`;
  }).join("");
})();

// Filters
const fDay = document.getElementById("f-day"), fCity = document.getElementById("f-city");
fDay.innerHTML = DAYS.map(d => `<option value="${d}">${fmtDay(d)}</option>`).reverse().join("");
fCity.innerHTML = `<option value="">All towns</option>` + CITIES.map(c => `<option>${c}</option>`).join("");
try { const s = JSON.parse(localStorage.getItem("wait-filters") || "{}");
  if (DAYS.includes(s.day)) fDay.value = s.day; if (CITIES.includes(s.city)) fCity.value = s.city; } catch (e) {}
function onFilter() {
  try { localStorage.setItem("wait-filters", JSON.stringify({ day: fDay.value, city: fCity.value })); } catch (e) {}
  renderHeat(); renderAvg(); renderTurnout(); renderUpdates();
}
fDay.onchange = fCity.onchange = onFilter;

function timeAxis(svg, x0, width, t0, t1, y, gridTop, gridBottom) {
  const span = Math.max(t1 - t0, 1), step = span > 360 ? 120 : span > 120 ? 60 : span > 60 ? 30 : 15;
  for (let m = Math.ceil(t0 / step) * step; m <= t1; m += step) {
    const x = x0 + (m - t0) / span * width;
    if (gridBottom != null) el("line", { x1: x, x2: x, y1: gridTop, y2: gridBottom, stroke: "var(--grid)" }, svg);
    text(svg, x, y, clock(m), { "text-anchor": "middle" });
  }
}

// Through the day: rows = sites grouped by town, columns = polls on the chosen day.
function renderHeat() {
  const host = document.getElementById("heat"); host.innerHTML = "";
  const d = fDay.value, city = fCity.value;
  const rows = ROWS.filter(r => day(r.t) === d && (!city || r.city === city));
  if (!rows.length) { host.innerHTML = `<p class="empty">No polls for this selection.</p>`; return; }
  const times = [...new Set(rows.map(r => r.t))].sort();
  const cell = {}; rows.forEach(r => cell[r.site + "|" + r.t] = r);
  const lines = [];
  (city ? [city] : CITIES).forEach(c => {
    const sites = Object.keys(siteCity).filter(s => siteCity[s] === c).sort();
    if (!sites.length) return;
    lines.push({ city: c });
    sites.forEach(s => lines.push({ site: s }));
  });
  const labelW = Math.min(300, Math.max(180, host.clientWidth * 0.32)), rowH = 18, cityH = 24, top = 4;
  const votesW = TURN ? 64 : 0;
  const plotW = Math.max(host.clientWidth - labelW - votesW - 8, times.length * 4);
  const t0 = mins(times[0]), t1 = mins(times[times.length - 1]);
  const span = Math.max(t1 - t0, 1);
  // Each poll covers until the next one, unless the monitor missed polls; then just one interval.
  const gap = POLL_GAP;
  const x = m => labelW + (m - t0) / (span + gap) * plotW;
  let y = top; lines.forEach(l => { l.y = y; y += l.city ? cityH : rowH; });
  const H = y + 22;
  const svg = el("svg", { width: labelW + plotW + votesW + 8, height: H, role: "img",
    "aria-label": `Wait times by site on ${fmtDay(d)}` }, host);
  const vx = labelW + plotW + votesW;
  lines.forEach((l, i) => {
    if (l.city) {
      text(svg, 0, l.y + cityH - 7, l.city, { class: "city" });
      if (TURN && i === 0) text(svg, vx, l.y + cityH - 7, "Early votes", { "text-anchor": "end" });
      return;
    }
    if (TURN) text(svg, vx, l.y + rowH / 2 + 4, fmtN((VOTES[l.site] || {})[d]), { class: "num", "text-anchor": "end" });
    const name = l.site.length > 38 ? l.site.slice(0, 36) + "…" : l.site;
    svgSiteLabel(svg, 12, l.y + rowH / 2 + 4, l.site, name);
    times.forEach((t, i) => {
      const r = cell[l.site + "|" + t];
      const m0 = mins(t), m1 = i + 1 < times.length ? mins(times[i + 1]) : null;
      const xa = x(m0), xb = m1 != null && !isGap(m0, m1) ? x(m1) : x(m0 + gap);
      const rect = el("rect", { x: xa, y: l.y + 1, width: Math.max(xb - xa - 1, 1), height: rowH - 2,
        rx: 2, fill: bandVar(r ? r.band : null) }, svg);
      hover(rect, () => `<b>${l.site}</b><span class="m">${fmtDay(d)} ${clock(mins(t))}</span><br>` +
        (r ? `${r.wait}${r.u ? `<br><span class="m">site last updated ${r.u.slice(0, 10) === d ? clock(mins(r.u)) : fmtDay(day(r.u)) + " " + clock(mins(r.u))}</span>` : ""}${r.min == null ? `<br><span class="m">not read as a time</span>` : ""}` : "No poll"));
    });
  });
  const axisY = y + 14;
  timeAxis(svg, labelW, plotW * span / (span + gap), t0, t1, axisY);
}

// Average estimated wait across sites (midpoint of each range), per poll.
function renderAvg() {
  const host = document.getElementById("avg"); host.innerHTML = "";
  const d = fDay.value, city = fCity.value;
  const rows = ROWS.filter(r => day(r.t) === d && (!city || r.city === city) && r.min != null);
  document.getElementById("avg-sub").textContent =
    `${city || "All towns"}, ${fmtDay(d)}. Each reported range counts as its midpoint (15–30 min → 22.5 min); an open-ended wait like "60+" counts as its lower bound.`;
  const kp = document.getElementById("kpis");
  if (!rows.length) { kp.innerHTML = ""; host.innerHTML = `<p class="empty">No polls for this selection.</p>`; return; }
  const byT = {}; rows.forEach(r => (byT[r.t] = byT[r.t] || []).push(r));
  const pts = Object.keys(byT).sort().map(t => ({ t, m: mins(t), v: mean(byT[t].map(r => r.min)), n: byT[t].length,
    long: byT[t].filter(r => r.min >= 30).length }));
  const peak = pts.reduce((a, p) => p.v > a.v ? p : a);
  const worst = rows.reduce((a, r) => r.min > a.min ? r : a);
  kp.innerHTML = [
    [fmtMin(mean(pts.map(p => p.v))), "average over the day"],
    [`${fmtMin(peak.v)} at ${clock(peak.m)}`, "busiest poll"],
    [worst.wait.replace(/ Minutes?/i, " min"), `longest single wait (${worst.site}, ${clock(mins(worst.t))})`],
  ].map(([v, k]) => `<div class="kpi"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");

  const W = Math.max(host.clientWidth, 320), H = 200, L = 40, R = 12, T = 10, B = 24;
  const t0 = pts[0].m, t1 = pts[pts.length - 1].m, span = Math.max(t1 - t0, 1);
  const vmax = Math.max(15, Math.ceil(Math.max(...pts.map(p => p.v)) / 15) * 15);
  const X = m => L + (m - t0) / span * (W - L - R), Y = v => T + (1 - v / vmax) * (H - T - B);
  const svg = el("svg", { width: W, height: H, role: "img", "aria-label": "Average estimated wait over the day" }, host);
  for (let v = 0; v <= vmax; v += 15) {
    el("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), stroke: v ? "var(--grid)" : "var(--axis)" }, svg);
    text(svg, L - 6, Y(v) + 4, v + "m", { "text-anchor": "end" });
  }
  timeAxis(svg, L, W - L - R, t0, t1, H - 6);
  // Break the line where the monitor missed polls rather than drawing across the gap.
  const runs = [[]];
  pts.forEach((p, i) => { if (i && isGap(pts[i - 1].m, p.m)) runs.push([]); runs[runs.length - 1].push(p); });
  runs.forEach(run => {
    const path = run.map((p, i) => `${i ? "L" : "M"}${X(p.m).toFixed(1)},${Y(p.v).toFixed(1)}`).join("");
    if (run.length > 1) el("path", { d: path + `L${X(run[run.length - 1].m)},${Y(0)}L${X(run[0].m)},${Y(0)}Z`,
      fill: "var(--line)", opacity: 0.12 }, svg);
    else el("circle", { cx: X(run[0].m), cy: Y(run[0].v), r: 2, fill: "var(--line)" }, svg);
    el("path", { d: path, fill: "none", stroke: "var(--line)", "stroke-width": 2, "stroke-linejoin": "round" }, svg);
  });
  const cross = el("line", { y1: T, y2: H - B, stroke: "var(--axis)", visibility: "hidden" }, svg);
  const dot = el("circle", { r: 4, fill: "var(--line)", stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }, svg);
  const hit = el("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" }, svg);
  hit.addEventListener("mousemove", e => {
    const bx = svg.getBoundingClientRect(), mx = e.clientX - bx.left;
    const p = pts.reduce((a, q) => Math.abs(X(q.m) - mx) < Math.abs(X(a.m) - mx) ? q : a);
    cross.setAttribute("x1", X(p.m)); cross.setAttribute("x2", X(p.m)); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", X(p.m)); dot.setAttribute("cy", Y(p.v)); dot.setAttribute("visibility", "visible");
    showTip(e, `<b>${clock(p.m)}</b>Average ${fmtMin(p.v)}<br><span class="m">${p.n} sites reporting · ${p.long} at 30+ min</span>`);
  });
  hit.addEventListener("mouseleave", () => { hideTip(); cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); });
}

// Early votes per day (State Board), following the Town filter; the chosen day is emphasized.
function renderTurnout() {
  const card = document.getElementById("turn-card");
  if (!TURN) { card.style.display = "none"; return; }
  const host = document.getElementById("turn"); host.innerHTML = "";
  const city = fCity.value, sel = fDay.value;
  const sites = Object.keys(VOTES).filter(s => !city || TURN_CITY[s] === city);
  const days = [...new Set(TURN.rows.map(r => r.d))].sort();
  const tot = days.map(d => ({ d, n: sites.reduce((a, s) => a + (VOTES[s][d] || 0), 0),
    top: sites.map(s => [s, VOTES[s][d] || 0]).filter(x => x[1]).sort((a, b) => b[1] - a[1]).slice(0, 3) }));
  document.getElementById("turn-sub").innerHTML =
    `${city || "All Wake County sites"}. Accepted in-person early votes from the ` +
    `<a href="${LINKS.ncsbe}" target="_blank" rel="noopener">NC State Board of Elections</a>, ` +
    `file updated ${TURN.updated}. The State Board posts each day's votes the next morning.`;
  const kp = document.getElementById("turn-kpis");
  if (!days.length) { kp.innerHTML = ""; host.innerHTML = `<p class="empty">No early votes reported yet. The first day's votes appear in the next morning's file.</p>`; return; }
  const total = tot.reduce((a, x) => a + x.n, 0), best = tot.reduce((a, x) => x.n > a.n ? x : a);
  const selN = tot.find(x => x.d === sel);
  kp.innerHTML = [[fmtN(total), `early votes over ${days.length} day${days.length > 1 ? "s" : ""}`],
    [fmtN(best.n), `busiest day (${fmtDay(best.d)})`],
    ...(selN ? [[fmtN(selN.n), `on ${fmtDay(sel)}`]] : [])]
    .map(([v, k]) => `<div class="kpi"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");

  const W = Math.max(host.clientWidth, 320), H = 200, L = 52, R = 8, T = 10, B = 24;
  const vmax = Math.max(...tot.map(x => x.n), 1);
  const stepRaw = vmax / 4, mag = 10 ** Math.floor(Math.log10(stepRaw));
  const step = [1, 2, 2.5, 5, 10].map(f => f * mag).find(x => x >= stepRaw), top = Math.ceil(vmax / step) * step;
  const Y = v => T + (1 - v / top) * (H - T - B), bw = (W - L - R) / days.length, barW = Math.max(Math.min(bw - 2, 40), 2);
  const svg = el("svg", { width: W, height: H, role: "img", "aria-label": "Early votes cast per day" }, host);
  for (let v = 0; v <= top; v += step) {
    el("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), stroke: v ? "var(--grid)" : "var(--axis)" }, svg);
    text(svg, L - 6, Y(v) + 4, fmtN(v), { "text-anchor": "end" });
  }
  const every = Math.ceil(days.length / Math.max(1, Math.floor((W - L - R) / 70)));
  tot.forEach((x, i) => {
    const cx = L + i * bw + bw / 2, h = Y(0) - Y(x.n), r = Math.min(4, barW / 2, h);
    const y0 = Y(0), xl = cx - barW / 2;
    // Rounded top, square baseline.
    el("path", { d: `M${xl},${y0}V${y0 - h + r}Q${xl},${y0 - h} ${xl + r},${y0 - h}H${xl + barW - r}` +
      `Q${xl + barW},${y0 - h} ${xl + barW},${y0 - h + r}V${y0}Z`,
      fill: !sel || x.d === sel || !tot.some(t => t.d === sel) ? "var(--line)" : "var(--line-soft)" }, svg);
    if (i % every === 0) text(svg, cx, H - 6, fmtDay(x.d).replace(/^\w+, /, ""), { "text-anchor": "middle" });
    const hit = el("rect", { x: L + i * bw, y: T, width: bw, height: H - T - B, fill: "transparent" }, svg);
    hover(hit, () => `<b>${fmtDay(x.d)}</b>${fmtN(x.n)} early votes` +
      (x.top.length ? `<br><span class="m">${x.top.map(([s, n]) => `${s}: ${fmtN(n)}`).join("<br>")}</span>` : ""));
    if (DAYS.includes(x.d)) { hit.style.cursor = "pointer"; hit.onclick = () => { fDay.value = x.d; onFilter(); }; }
  });
}

// Site updates. A site's update times are the distinct "last updated" stamps we saw. A gap
// between two updates only counts if the monitor was polling the whole time; otherwise an
// update in between could have been missed.
const median = a => { if (!a.length) return null; const b = [...a].sort((x, y) => x - y), h = b.length >> 1;
  return b.length % 2 ? b[h] : (b[h - 1] + b[h]) / 2; };
const fmtDur = m => m == null ? "—" : m < 60 ? `${Math.round(m)} min` : `${Math.floor(m / 60)} h ${String(Math.round(m % 60)).padStart(2, "0")} min`;
function pollGaps(d) {  // [start, end] minute ranges on day d when the monitor wasn't polling
  const ts = [...new Set(ROWS.filter(r => day(r.t) === d).map(r => mins(r.t)))].sort((a, b) => a - b), out = [];
  for (let i = 1; i < ts.length; i++) if (isGap(ts[i - 1], ts[i])) out.push([ts[i - 1], ts[i]]);
  return { first: ts[0], last: ts[ts.length - 1], gaps: out };
}
function siteUpdates(site, d) {
  // Updates stamped on day d, each with the wait it set (from the first poll that saw it).
  const seen = new Map();
  ROWS.filter(r => r.site === site && day(r.t) === d && r.u && day(r.u) === d).sort((a, b) => a.t < b.t ? -1 : 1)
    .forEach(r => { if (!seen.has(r.u)) seen.set(r.u, r); });
  const ups = [...seen.entries()].map(([u, r]) => ({ m: mins(u), wait: r.wait })).sort((a, b) => a.m - b.m);
  const pg = pollGaps(d), intervals = [];
  ups.forEach((x, i) => {
    if (!i) return;
    const a = ups[i - 1].m, b = x.m;
    // Count it only if we were polling throughout (an earlier update could hide behind the first poll or a gap).
    x.since = pg.gaps.some(([g0, g1]) => g0 < b && g1 > a) || a < pg.first - POLL_GAP ? null : b - a;
    if (x.since != null) intervals.push(x.since);
  });
  return { ups, intervals };
}
function estimateAges(rows) {  // minutes between a site's last update and our poll, same day only
  return rows.filter(r => r.u && day(r.u) === day(r.t)).map(r => mins(r.t) - mins(r.u)).filter(a => a >= 0);
}

function renderUpdates() {
  const host = document.getElementById("upd"); host.innerHTML = "";
  const d = fDay.value, city = fCity.value;
  const sites = (city ? [city] : CITIES).flatMap(c => Object.keys(siteCity).filter(s => siteCity[s] === c).sort());
  const per = Object.fromEntries(sites.map(s => [s, siteUpdates(s, d)]));
  const all = sites.flatMap(s => per[s].intervals);
  const dayRows = ROWS.filter(r => day(r.t) === d && sites.includes(r.site));
  const ages = estimateAges(dayRows);
  document.getElementById("upd-sub").textContent =
    `${city || "All towns"}, ${fmtDay(d)}. Each tick is a time the site changed or re-confirmed its estimate. ` +
    `Gaps are only measured while the monitor was polling (shaded where it wasn't), so updates less than ` +
    `${POLL_GAP} min apart can be missed.`;
  const slow = sites.map(s => [s, median(per[s].intervals)]).filter(x => x[1] != null).sort((a, b) => b[1] - a[1])[0];
  document.getElementById("upd-kpis").innerHTML = [
    [fmtDur(median(all)), "typical time between updates"],
    [ages.length ? Math.round(ages.filter(a => a >= 30).length / ages.length * 100) + "%" : "—", "of polls showed an estimate 30+ min old"],
    ...(slow ? [[fmtDur(slow[1]), `slowest to update (${slow[0]}, typical gap)`]] : []),
  ].map(([v, k]) => `<div class="kpi"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
  if (!dayRows.length) { host.innerHTML = `<p class="empty">No polls for this selection.</p>`; return; }

  const pg = pollGaps(d);
  const t0 = Math.min(pg.first, ...sites.flatMap(s => per[s].ups.map(u => u.m))), t1 = pg.last;
  const labelW = Math.min(300, Math.max(180, host.clientWidth * 0.32)), rowH = 18, cityH = 24, colW = 78;
  const plotW = Math.max(host.clientWidth - labelW - colW * 3 - 8, 200);
  const span = Math.max(t1 - t0, 1), X = m => labelW + (m - t0) / span * plotW;
  const lines = [];
  (city ? [city] : CITIES).forEach(c => { lines.push({ city: c });
    Object.keys(siteCity).filter(s => siteCity[s] === c).sort().forEach(s => lines.push({ site: s })); });
  let y = 4; lines.forEach(l => { l.y = y; y += l.city ? cityH : rowH; });
  const H = y + 22, W = labelW + plotW + colW * 3 + 8;
  const svg = el("svg", { width: W, height: H, role: "img", "aria-label": `Estimate updates by site on ${fmtDay(d)}` }, host);
  // Shade before the first poll and during monitor gaps: updates there may be missing.
  [[t0, pg.first], ...pg.gaps].forEach(([a, b]) => { if (b > a)
    el("rect", { x: X(a), y: 4, width: X(b) - X(a), height: y - 4, class: "gap", opacity: 0.6 }, svg); });
  const cx = i => labelW + plotW + colW * (i + 1);
  lines.forEach((l, i) => {
    if (l.city) {
      text(svg, 0, l.y + cityH - 7, l.city, { class: "city" });
      if (i === 0) ["Updates", "Typical gap", "Longest gap"].forEach((h, j) => text(svg, cx(j), l.y + cityH - 7, h, { "text-anchor": "end" }));
      return;
    }
    const yc = l.y + rowH / 2, u = per[l.site];
    const name = l.site.length > 38 ? l.site.slice(0, 36) + "…" : l.site;
    svgSiteLabel(svg, 12, yc + 4, l.site, name);
    el("line", { x1: labelW, x2: labelW + plotW, y1: yc, y2: yc, stroke: "var(--grid)" }, svg);
    u.ups.forEach(x => {
      el("rect", { x: X(x.m) - 1.5, y: l.y + 3, width: 3, height: rowH - 6, rx: 1.5, class: "tick" }, svg);
      const hit = el("rect", { x: X(x.m) - 5, y: l.y, width: 10, height: rowH, fill: "transparent" }, svg);
      hover(hit, () => `<b>${l.site}</b>Updated ${clock(x.m)} → ${x.wait}` +
        (x.since != null ? `<br><span class="m">${fmtDur(x.since)} after the previous update</span>` : ""));
    });
    [String(u.ups.length), fmtDur(median(u.intervals)), fmtDur(u.intervals.length ? Math.max(...u.intervals) : null)]
      .forEach((v, j) => text(svg, cx(j), yc + 4, v, { class: "num", "text-anchor": "end" }));
  });
  timeAxis(svg, labelW, plotW, t0, t1, y + 14);
}

// Typical by time of day: town x half-hour, averaged over every poll on every day.
(function renderTod() {
  const host = document.getElementById("tod");
  const ok = ROWS.filter(r => r.min != null);
  document.getElementById("tod-sub").textContent =
    `Mean estimated wait in each half hour across all ${DAYS.length} day${DAYS.length > 1 ? "s" : ""} collected and all sites in the town. ` +
    (DAYS.length < 3 ? "This view gets more useful as more days are collected." : "");
  if (!ok.length) { host.innerHTML = `<p class="empty">No data.</p>`; return; }
  const slot = m => Math.floor(m / 30) * 30;
  const acc = {}; ok.forEach(r => { const k = r.city + "|" + slot(mins(r.t)); (acc[k] = acc[k] || []).push(r.min); });
  const slots = [...new Set(ok.map(r => slot(mins(r.t))))].sort((a, b) => a - b);
  const s0 = slots[0], s1 = slots[slots.length - 1], cols = (s1 - s0) / 30 + 1;
  const labelW = 130, rowH = 22, top = 4;
  const cw = Math.max(18, Math.min(44, (host.clientWidth - labelW - 8) / cols));
  const W = labelW + cols * cw + 8, H = top + CITIES.length * rowH + 22;
  const svg = el("svg", { width: W, height: H, role: "img", "aria-label": "Typical wait by town and time of day" }, host);
  CITIES.forEach((c, i) => {
    const y = top + i * rowH;
    text(svg, 0, y + rowH / 2 + 4, `${c} (${cityCount[c]})`, { class: "lbl" });
    for (let j = 0; j < cols; j++) {
      const m = s0 + j * 30, vals = acc[c + "|" + m], v = vals ? mean(vals) : null;
      const rect = el("rect", { x: labelW + j * cw, y: y + 1, width: cw - 2, height: rowH - 2, rx: 3,
        fill: bandVar(bandOf(v)) }, svg);
      hover(rect, () => `<b>${c}, ${clock(m)}–${clock(m + 30)}</b>` +
        (vals ? `Average ${fmtMin(v)}<br><span class="m">${vals.length} observations</span>` : "No data"));
    }
  });
  for (let j = 0; j < cols; j++) {
    const m = s0 + j * 30;
    if (m % 60 === 0 && (cw >= 30 || m % 120 === 0)) text(svg, labelW + j * cw, H - 6, clock(m), { "text-anchor": "start" });
  }
})();

// Site summary table (sortable), across all collected data.
(function renderTable() {
  const by = {};
  ROWS.forEach(r => (by[r.site] = by[r.site] || []).push(r));
  const recs = Object.entries(by).map(([site, rs]) => {
    rs.sort((a, b) => a.t < b.t ? -1 : 1);
    const vals = rs.filter(r => r.min != null);
    const latest = rs[rs.length - 1];
    return { site, city: siteCity[site], latest: latest.wait, latestMin: latest.min ?? -1,
      avg: mean(vals.map(r => r.min)), peak: vals.length ? Math.max(...vals.map(r => r.min)) : null,
      long: vals.length ? vals.filter(r => r.min >= 30).length / vals.length : null,
      none: vals.length ? vals.filter(r => r.min === 0).length / vals.length : null,
      votes: VOTES[site] ? Object.values(VOTES[site]).reduce((a, n) => a + n, 0) : null,
      gap: median(DAYS.flatMap(d => siteUpdates(site, d).intervals)), age: median(estimateAges(rs)) };
  });
  document.getElementById("tbl-sub").textContent = `All ${DAYS.length} day${DAYS.length > 1 ? "s" : ""}. Click a column to sort.`;
  const pct = v => v == null ? "—" : Math.round(v * 100) + "%";
  const cols = [
    ["site", "Site", r => siteLink(r.site)], ["city", "Town", r => r.city],
    ["latestMin", "Latest", r => r.latest, 1], ["avg", "Avg wait", r => fmtMin(r.avg), 1],
    ["peak", "Peak (midpoint)", r => fmtMin(r.peak), 1], ["long", "Polls at 30+ min", r => pct(r.long), 1],
    ["none", "Polls with no wait", r => pct(r.none), 1],
    ["gap", "Typical update gap", r => fmtDur(r.gap), 1], ["age", "Typical estimate age", r => fmtDur(r.age), 1],
    ...(TURN ? [["votes", "Early votes", r => fmtN(r.votes), 1]] : []),
  ];
  let key = "avg", dir = -1;
  const tbl = document.getElementById("tbl");
  function draw() {
    recs.sort((a, b) => { const x = a[key] ?? -1, y = b[key] ?? -1;
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir; });
    tbl.innerHTML = `<thead><tr>${cols.map(([k, h, , n]) =>
      `<th data-k="${k}" class="${n ? "num" : ""}" aria-sort="${k === key ? (dir > 0 ? "ascending" : "descending") : "none"}">${h}${k === key ? (dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead>` +
      `<tbody>${recs.map(r => `<tr>${cols.map(([k, , f, n]) => `<td class="${n ? "num" : ""}">${f(r)}</td>`).join("")}</tr>`).join("")}</tbody>`;
    tbl.querySelectorAll("th").forEach(th => th.onclick = () => {
      const k = th.dataset.k; dir = k === key ? -dir : (k === "site" || k === "city" ? 1 : -1); key = k; draw(); });
  }
  draw();
})();

// Data notes: polling coverage per day (gaps found automatically) plus --note text.
(function renderNotes() {
  const items = DAYS.map(d => {
    const pg = pollGaps(d);
    const gaps = pg.gaps.map(([a, b]) => `${clock(a)}–${clock(b)}`);
    return `<b>${fmtDay(d)}:</b> polled ${clock(pg.first)}–${clock(pg.last)}` +
      (gaps.length ? `; no polls ${gaps.join(", ")} (the monitor wasn't running, so waits and site updates in ` +
        `${gaps.length > 1 ? "those stretches" : "that stretch"} weren't recorded and show as no data)` : "; no gaps");
  });
  const esc = t => t.replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  document.getElementById("notes").innerHTML = [...(DATA.notes || []).map(esc), ...items,
    `Polls about every ${POLL_GAP} min; a gap is any stretch over ${POLL_GAP * 3} min without a poll. ` +
    `Source: ${DATA.sources.map(esc).join(", ")}.`].map(t => `<li>${t}</li>`).join("");
})();

onFilter();
let rz; addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { renderHeat(); renderAvg(); renderTurnout(); renderUpdates(); }, 150); });
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
