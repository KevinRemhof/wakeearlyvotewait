#!/usr/bin/env python3
"""Build the statewide 2026 NC early voting report: who is voting, when, and where
compared with 2024 and 2022, and how voters moved as one-stop sites changed.

Everything comes from NC State Board of Elections ballot records (the daily
statewide absentee file plus earlier federal elections, see
build_ballot_history.py). The page shows aggregates only, never individual voters.

    python3 nc_voting_report.py                 # rebuild if the State Board posted a newer file
    python3 nc_voting_report.py --force --open  # rebuild regardless and open it

Exits 0 after writing the report, 3 when the report is already current.
Standard library only.
"""

import argparse
import collections
import csv
import gzip
import json
import re
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

import analyze_2026_voters as an
import build_site_crosswalk
import build_site_results
import geocode_sites
from visualize_waits import NCSBE_URL, TZ, fetch_absentee

HERE = Path(__file__).resolve().parent
OUT = HERE / "docs" / "2026_NC_Voting" / "index.html"
UP_TO_DATE = 3
STAMP = '<meta name="source-modified" content="{}">'


def source_stamp(html):
    start = html.find('<meta name="source-modified" content="')
    if start < 0:
        return None
    start += len('<meta name="source-modified" content="')
    return html[start : html.find('"', start)]


def site_changes():
    """{"2024": {county: {added, removed, renamed}}, "2022": ...} vs 2026, ids only."""
    ch = json.load(open(an.CACHE / "onestop_site_changes.json"))["changes"]
    out = {}
    for e in an.COMPARE:
        out[e[:4]] = {
            county: {k: [s["id"] for s in v[k]] for k in ("added", "removed", "renamed")}
            for county, v in ch[f"{e}_to_{an.E26}"].items()
        }
    return out


def older_years(sites):
    """{site id: ["2014", ...]}: earlier federal elections (before 2022, which have no site
    lists) with one-stop ballots at a site of the same name in the same county."""
    hist, cache = an.CACHE / "ballots_federal.tsv.gz", an.CACHE / "onestop_site_names_pre2022.json"
    if not cache.exists() or cache.stat().st_mtime < hist.stat().st_mtime:
        names = collections.Counter()
        with gzip.open(hist, "rt", newline="") as f:
            for r in csv.DictReader(f, delimiter="\t"):
                if r["method"] == "ONESTOP" and r["election"] < an.COMPARE[-1] and r["site_name"]:
                    names[(r["election"], r["county"], r["site_name"])] += 1
        cache.write_text(json.dumps([[*k, n] for k, n in names.items()]))
    norm = lambda n: re.sub(r"[^A-Z0-9]", "", re.sub(r"^[A-Z]{2} - ", "", n.upper()))
    by_name = {}
    for s in json.loads((an.CACHE / "onestop_site_crosswalk.json").read_text())["sites"]:
        if s["id"] in sites:
            for e in s["elections"].values():
                for n in (e.get("site_name"), e.get("official_list_name")):
                    if n:
                        by_name[(s["county"], norm(n))] = s["id"]
    out = collections.defaultdict(set)
    for el, county, name, n in json.loads(cache.read_text()):
        sid = by_name.get((county, norm(name)))
        if sid and n >= 25:     # a handful of ballots under an old name isn't a site that was open
            out[sid].add(el[:4])
    return {k: sorted(v) for k, v in out.items()}


def site_results(used):
    """{"2024": {site id: {DEM, REP, OTHER}}, ...} for counties that report early votes by site."""
    build_site_results.main(verbose=False)
    res = json.loads((an.CACHE / "onestop_site_results.json").read_text())
    return {el[:4]: {k: {p: v[p] for p in ("DEM", "REP", "OTHER")} for k, v in sites.items() if k in used}
            for el, sites in res["elections"].items()}


def build(out, modified):
    build_site_crosswalk.main()
    coords = geocode_sites.geocode_all()
    res = an.analyze()
    changes = site_changes()
    used = set(res["sites26"])
    for y in res["flows"]:
        for src, dests in res["flows"][y].items():
            used.add(src)
            used.update(dests)
    for y in changes:
        for v in changes[y].values():
            for ids in v.values():
                used.update(ids)
    data = {
        "generated": datetime.now(TZ).strftime("%a %b %-d, %Y %-I:%M %p"),
        "source_modified": modified.strftime("%a %b %-d, %Y %-I:%M %p"),
        "through": res["through"],
        "scopes": res["scopes"],
        "prior_daily": res["prior_daily"],
        "flows": res["flows"],
        "flow_timing": res["flow_timing"],
        "coords": {k: [round(v[0], 4), round(v[1], 4)] for k, v in coords.items() if k in used},
        "sites26": res["sites26"],
        "sites": {k: v for k, v in res["site_info"].items() if k in used},
        "older_years": older_years(used),
        "results": site_results(used),
        "result_contests": {el[:4]: c.title().replace("Us ", "U.S. ") for el, c in build_site_results.CONTESTS.items()},
        "changes": changes,
        "unmatched26": sum(res["unmatched26"].values()),
        "links": {"ncsbe": NCSBE_URL, "wait": "../"},
    }
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.replace("/*__DATA__*/null", payload).replace(
        "<!--__STAMP__-->", STAMP.format(modified.astimezone(UTC).isoformat())
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    nc = res["scopes"]["NC"]
    print(f"wrote {out} ({nc['n']:,} ballots through {res['through']}; source {modified:%c})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-o", "--output", type=Path, default=OUT)
    ap.add_argument("--force", action="store_true", help="rebuild even if the source is unchanged")
    ap.add_argument("--no-fetch", action="store_true", help="use the cached State Board file")
    ap.add_argument("--open", action="store_true", help="open the report in a browser")
    args = ap.parse_args()

    if args.no_fetch:
        path = an.CACHE / f"absentee_{an.E26}.zip"
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC).astimezone(TZ)
    else:
        path, modified = fetch_absentee(an.E26, an.CACHE)
    if not path:
        sys.exit("no State Board absentee file available")
    stamp = modified.astimezone(UTC).isoformat()
    if not args.force and args.output.exists() and source_stamp(args.output.read_text()) == stamp:
        print(f"report already uses the State Board file from {modified:%c}; nothing to do")
        sys.exit(UP_TO_DATE)
    build(args.output, modified)
    if args.open:
        webbrowser.open(args.output.resolve().as_uri())


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!--__STAMP__-->
<title>2026 NC Early Voting</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --none: #ecebe6; --mid: #d6d5ce;
  --y2026: #199e70; --y2024: #4a3aa7; --y2022: #eb6834;
  --bar: #199e70; --link: #0b7a55;
  --same: #199e70; --open: #2a78d6; --closed: #eb6834; --county: #a8a79f;
  --ed: #066f4d; --earlier: #22af7d; --later: #eb6834;
  --dem: #2e6bd1; --rep: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --none: #262624; --mid: #4a4a46;
    --y2026: #199e70; --y2024: #9085e9; --y2022: #d95926;
    --bar: #199e70; --link: #22af7d;
    --same: #199e70; --open: #3987e5; --closed: #d95926; --county: #6b6a64;
    --ed: #75c6a1; --earlier: #078f64; --later: #d95926;
    --dem: #5b8fe8; --rep: #e5615f;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19;
  --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --none: #262624; --mid: #4a4a46;
  --y2026: #199e70; --y2024: #9085e9; --y2022: #d95926;
  --bar: #199e70; --link: #22af7d;
  --same: #199e70; --open: #3987e5; --closed: #d95926; --county: #6b6a64;
  --ed: #75c6a1; --earlier: #078f64; --later: #d95926;
  --dem: #5b8fe8; --rep: #e5615f;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 0 0 2px; }
h3 { font-size: 13px; margin: 14px 0 6px; color: var(--ink-2); font-weight: 600; }
.sub { color: var(--ink-2); margin: 0 0 12px; max-width: 75ch; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px;
  padding: 16px; margin-top: 16px; }
.filters { display: flex; flex-wrap: wrap; gap: 16px; align-items: center; margin-top: 16px; }
.filters label { color: var(--ink-2); font-size: 13px; display: flex; gap: 6px; align-items: center; }
select { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--axis);
  border-radius: 6px; padding: 4px 8px; max-width: 100%; }
.seg { display: inline-flex; border: 1px solid var(--axis); border-radius: 6px; overflow: hidden; }
.seg button { font: inherit; color: var(--ink-2); background: var(--surface); border: 0; padding: 4px 12px; cursor: pointer; }
.seg button + button { border-left: 1px solid var(--axis); }
.seg button.on { color: var(--ink); font-weight: 600; background: var(--none); }
.foot { color: var(--muted); font-size: 12px; margin: 10px 0 0; max-width: 85ch; }
.disclaimer { color: var(--muted); font-size: 12px; text-align: center; margin: 24px 0 0; }
.links { margin: -4px 0 0; font-size: 13px; color: var(--ink-2); }
.links a { color: var(--link); font-weight: 600; text-decoration: none; margin-right: 16px; }
.links a:hover { text-decoration: underline; }
a { color: inherit; text-decoration-color: var(--axis); text-underline-offset: 2px; }
.kpis { display: flex; flex-wrap: wrap; gap: 28px; margin: 4px 0; }
.kpi .v { font-size: 28px; font-weight: 600; }
.kpi.hero .v { font-size: 48px; line-height: 1.1; }
.kpi .k { color: var(--ink-2); font-size: 12px; max-width: 22ch; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--ink-2); margin: 8px 0 4px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.sw { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
.lk { width: 16px; height: 2px; border-radius: 1px; display: inline-block; }
svg { display: block; overflow: visible; }
svg text { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
svg .end { fill: var(--ink-2); font-size: 12px; }
.grid3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 8px 32px; }
.hbar { display: grid; grid-template-columns: fit-content(60%) 1fr; gap: 6px 10px; align-items: center; font-size: 13px; }
.hbar .l { color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hbar .t { display: flex; align-items: center; gap: 6px; min-width: 0; }
.hbar .b { height: 14px; background: var(--bar); border-radius: 0 4px 4px 0; min-width: 2px; }
.hbar .n { color: var(--ink-2); font-variant-numeric: tabular-nums; white-space: nowrap; font-size: 12px; }
.stack-row { display: grid; grid-template-columns: 64px 1fr; gap: 10px; align-items: center; margin: 8px 0; }
.stack-row .y { font-weight: 600; font-size: 13px; }
.stack { display: flex; gap: 2px; height: 26px; }
.stack div { height: 100%; display: flex; align-items: center; justify-content: center; overflow: visible;
  font-size: 12px; font-weight: 600; white-space: nowrap; min-width: 2px; }
.stack div:first-child { border-radius: 4px 0 0 4px; }
.stack div:last-child { border-radius: 0 4px 4px 0; }
.stack div:only-child { border-radius: 4px; }
.mini { display: flex; gap: 2px; height: 10px; width: 140px; }
.mini div { height: 100%; min-width: 1px; }
.mini div:first-child { border-radius: 3px 0 0 3px; }
.mini div:last-child { border-radius: 0 3px 3px 0; }
.scroll { overflow-x: auto; }
.tablewrap { max-height: 520px; overflow: auto; border-top: 1px solid var(--grid); }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--ink-2); font-weight: 600; font-size: 12px; position: sticky; top: 0; background: var(--surface); z-index: 1; }
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { color: var(--ink); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tr.pick { cursor: pointer; }
tr.pick:hover td { background: var(--none); }
tr.sel td { background: var(--none); font-weight: 600; }
.badge { display: inline-block; font-size: 11px; font-weight: 600; padding: 0 6px; border-radius: 4px;
  border: 1px solid var(--axis); color: var(--ink-2); margin-left: 6px; white-space: nowrap; }
.addr { color: var(--muted); font-size: 12px; }
.dest { color: var(--ink-2); font-size: 12px; }
tr.group td { background: var(--none); font-weight: 600; padding-top: 10px; }
tr.group .n { color: var(--muted); font-weight: 400; font-size: 12px; margin-left: 8px; }
details { margin-top: 10px; }
summary { cursor: pointer; color: var(--ink-2); font-size: 13px; }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 8px 32px; }
.cols ul { margin: 4px 0 0; padding-left: 18px; }
.cols li { margin: 2px 0; }
.empty { color: var(--muted); padding: 8px 0; }
#tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink);
  border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; font-size: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,.15); display: none; z-index: 10; max-width: 300px; }
#tip .r { display: flex; align-items: center; gap: 6px; }
#tip b { font-variant-numeric: tabular-nums; }
#tip .m { color: var(--ink-2); }
</style>
</head>
<body>
<main>
  <h1>2026 North Carolina early voting: who's voting, when and where</h1>
  <p class="sub" id="meta"></p>
  <p class="links" id="links"></p>

  <div class="filters">
    <label>Area <select id="f-county"></select></label>
    <label>Compare with <span class="seg" id="f-cmp"><button data-y="2024">2024</button><button data-y="2022">2022</button></span></label>
  </div>

  <section class="card" aria-labelledby="h-kpi">
    <h2 id="h-kpi">Ballots cast so far</h2>
    <p class="sub" id="kpi-sub"></p>
    <div class="kpis" id="kpis"></div>
  </section>

  <section class="card" aria-labelledby="h-daily">
    <h2 id="h-daily">One-stop ballots by day of early voting</h2>
    <p class="sub" id="daily-sub"></p>
    <div class="legend" id="daily-legend"></div>
    <div id="daily"></div>
    <details><summary>Table</summary><div class="scroll" id="daily-table"></div></details>
  </section>

  <section class="card" aria-labelledby="h-hist">
    <h2 id="h-hist">One-stop ballots by day, every federal general election since 2002</h2>
    <p class="sub" id="hist-sub"></p>
    <div class="filters" style="margin-top:0">
      <span class="seg" id="hist-kind"><button data-v="midterm">Midterms</button><button data-v="pres">Presidential</button><button data-v="all">All</button></span>
      <span class="seg" id="hist-measure"><button data-v="day">Per day</button><button data-v="cum">Running total</button></span>
    </div>
    <div class="legend" id="hist-legend"></div>
    <div id="hist"></div>
    <p class="foot">Days before Election Day, which falls on a Tuesday every year, so each day lines up by weekday.
      Early voting ran 19 to 3 days out in most years, 20 to 3 in 2018, and 12 to 3 in 2014. Counts are one-stop ballots
      from the State Board's absentee files, by the date each was cast; Election Day and mail ballots aren't included.</p>
    <details><summary>Table</summary><div class="scroll" id="hist-table"></div></details>
  </section>

  <section class="card" aria-labelledby="h-first">
    <h2 id="h-first">First-time one-stop voters</h2>
    <p class="sub" id="first-sub"></p>
    <div class="grid3" id="first"></div>
  </section>

  <section class="card" aria-labelledby="h-timing">
    <h2 id="h-timing">Voting earlier or later than before</h2>
    <p class="sub" id="usual-sub"></p>
    <div class="legend" id="usual-legend"></div>
    <div id="usual"></div>
    <h3>Compared with each election</h3>
    <p class="sub" id="timing-sub"></p>
    <div class="legend" id="timing-legend"></div>
    <div id="timing"></div>
    <p class="foot" id="timing-foot"></p>
  </section>

  <section class="card" aria-labelledby="h-where">
    <h2 id="h-where">Same site, or a different one?</h2>
    <p class="sub" id="where-sub"></p>
    <div class="legend" id="where-legend"></div>
    <div id="where"></div>
    <p class="foot">Sites are matched by street address, so a building that was renamed counts as the same site.
      Only voters who voted one-stop in both elections are included.</p>
  </section>

  <section class="card" aria-labelledby="h-flows">
    <h2 id="h-flows">Closed Early Voting Sites</h2>
    <p class="sub" id="flows-sub"></p>
    <div class="kpis" id="flows-kpis"></div>
    <div class="legend" id="flows-legend"></div>
    <div class="tablewrap" id="flows"></div>
    <p class="foot" id="flows-foot"></p>
  </section>

  <section class="card" aria-labelledby="h-sites">
    <h2 id="h-sites">2026 one-stop sites: who is voting there</h2>
    <p class="sub" id="sites-sub"></p>
    <div class="tablewrap" id="sites"></div>
  </section>

  <section class="card" aria-labelledby="h-changes">
    <h2 id="h-changes"></h2>
    <p class="sub" id="changes-sub"></p>
    <div class="cols" id="changes"></div>
  </section>

  <section class="card" aria-labelledby="h-part">
    <h2 id="h-part">How often 2026 voters voted in the last five federal elections</h2>
    <p class="sub">2016, 2018, 2020, 2022 and 2024 general elections, any method, including Election Day.</p>
    <div id="part"></div>
  </section>

  <section class="card" aria-labelledby="h-counties">
    <h2 id="h-counties">By county</h2>
    <p class="sub" id="counties-sub">Select a row to see that county throughout the page. Select a column heading to sort.</p>
    <div class="tablewrap" id="counties"></div>
  </section>

  <section class="card" aria-labelledby="h-method">
    <h2 id="h-method">About the data</h2>
    <ul class="foot" id="method">
      <li>Everything on this page comes from the NC State Board of Elections' statewide absentee ballot files: one record per
        one-stop (early, in-person) or mail ballot, for every federal general election from 2002 to 2026. Election Day votes
        from 2016 on come from the State Board's voter history file. Only accepted ballots count.</li>
      <li>The 2026 file is refreshed by the State Board early each morning with the previous day's ballots, and this page is
        rebuilt as soon as a new file is posted.</li>
      <li>Voters are followed across elections by their State Board voter ID (NCID). Records before 2016 don't carry it and
        are linked by county and county registration number, so voters who changed counties before 2016 can lose some
        earlier history.</li>
      <li><b>First-time one-stop</b> means no one-stop ballot in any federal general election from 2002 to 2024. Primaries
        and municipal elections aren't counted.</li>
      <li><b>Earlier or later</b> compares how many days before Election Day the ballot was cast. Early voting opened 19 days
        before Election Day in 2022, 2024 and 2026, so day 1 lines up in all three years.</li>
      <li>One-stop sites are matched across years by street address from the State Board's published site lists.</li>
      <li>This page shows totals only, never individual voters.</li>
    </ul>
  </section>

  <footer class="disclaimer">This page uses data from the North Carolina State Board of Elections but is not affiliated
    with or endorsed by it.</footer>
</main>
<div id="tip" role="tooltip"></div>
<script>
const D = /*__DATA__*/null;
const YEARS = ["2022", "2024", "2026"];
const EDAY = {"2002": "2002-11-05", "2004": "2004-11-02", "2006": "2006-11-07", "2008": "2008-11-04", "2010": "2010-11-02", "2012": "2012-11-06", "2014": "2014-11-04", "2016": "2016-11-08", "2018": "2018-11-06", "2020": "2020-11-03", "2022": "2022-11-08", "2024": "2024-11-05", "2026": "2026-11-03"};
const EV_DAYS = 17, EV_FIRST = 19;  // early voting: 19 through 3 days before Election Day
const fmtN = n => n == null ? "—" : Math.round(n).toLocaleString();
const pct = (n, d) => d ? 100 * n / d : null;
const fmtP = (p, digits = 1) => p == null ? "—" : `${p.toFixed(digits)}%`;
const NS = "http://www.w3.org/2000/svg";
const $ = id => document.getElementById(id);
const ACRONYMS = new Set(["NC", "NCSU", "UNC", "UNCC", "YMCA", "YWCA", "EMS", "CVCC", "CFCC", "NCRC", "VFD", "AME", "US", "HWY", "II", "III", "ISS", "CATS"]);
const titleCase = s => s.toLowerCase().replace(/\b([a-z])/g, c => c.toUpperCase())
  .replace(/\b[A-Za-z]+\b/g, w => ACRONYMS.has(w.toUpperCase()) ? w.toUpperCase() : w);
const dateOf = (y, dbe) => { const d = new Date(EDAY[y] + "T12:00"); d.setDate(d.getDate() - dbe); return d; };
const fmtDate = d => d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });

// DOM helper: h("td", {class: "num"}, "text", childNode, ...). Strings become text nodes.
function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "style") Object.assign(e.style, v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k instanceof Node ? k : String(k));
  return e;
}
function s(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
const fill = (id, ...kids) => { const e = $(id); e.replaceChildren(...kids.flat()); return e; };

// Tooltip: rows of [color or null, value, label]; built with text nodes only.
const tip = $("tip");
function showTip(ev, title, rows) {
  tip.replaceChildren(h("div", { class: "m" }, title),
    ...rows.map(([c, v, l]) => h("div", { class: "r" },
      c ? h("span", { class: "lk", style: { background: c } }) : null, h("b", {}, v), h("span", { class: "m" }, l))));
  tip.style.display = "block";
  const x = Math.min(ev.clientX + 14, innerWidth - tip.offsetWidth - 8);
  const y = ev.clientY + 14 + tip.offsetHeight > innerHeight ? ev.clientY - tip.offsetHeight - 10 : ev.clientY + 14;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
const hideTip = () => { tip.style.display = "none"; };
function tipOn(el, title, rows) {
  el.tabIndex = 0;
  const show = ev => showTip(ev.clientX != null ? ev : { clientX: el.getBoundingClientRect().left, clientY: el.getBoundingClientRect().bottom }, title, rows);
  el.addEventListener("pointermove", show); el.addEventListener("focus", show);
  el.addEventListener("pointerleave", hideTip); el.addEventListener("blur", hideTip);
}

// ---- state (kept in the URL hash so a view can be shared) ----
let county = "NC", cmp = "2024";
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  if (p.get("county") && D.scopes[p.get("county")]) county = p.get("county");
  if (YEARS.includes(p.get("cmp")) && p.get("cmp") !== "2026") cmp = p.get("cmp");
}
function writeHash() { history.replaceState(null, "", `#county=${encodeURIComponent(county)}&cmp=${cmp}`); }
const S = () => D.scopes[county];
const areaName = () => county === "NC" ? "North Carolina" : `${titleCase(county)} County`;
const inArea = id => county === "NC" || (D.sites[id] && D.sites[id].county === county);
const siteName = id => D.sites[id] ? titleCase(D.sites[id].name.replace(/^[A-Z]{2} - /, "")) : "Unmatched site";
const siteAddr = id => { const a = D.sites[id] && D.sites[id].address; return a ? `${titleCase(a.street)}, ${titleCase(a.city)}` : ""; };
const openIn = (id, y) => D.sites[id] && D.sites[id].years.includes(y);
const evDay = () => D.through ? Math.round((new Date(EDAY["2026"] + "T12:00") - new Date(D.through + "T12:00")) / 864e5) : null;

// ---- header ----
function renderHeader() {
  const dbe = evDay(), day = dbe == null ? null : EV_FIRST - dbe + 1;
  fill("meta", `Ballots through ${D.through ? fmtDate(new Date(D.through + "T12:00")) : "—"}`,
    day ? ` (day ${day} of ${EV_DAYS} of early voting)` : "",
    `. State Board file posted ${D.source_modified}; page built ${D.generated}.`);
  fill("links", h("a", { href: D.links.ncsbe, target: "_blank", rel: "noopener" }, "State Board absentee data"),
    h("a", { href: D.links.wait }, "Wake County wait times"));
}

function renderFilters() {
  const sel = $("f-county");
  const names = Object.keys(D.scopes).filter(k => k !== "NC").sort();
  sel.replaceChildren(h("option", { value: "NC" }, "Statewide"), ...names.map(c => h("option", { value: c }, titleCase(c))));
  sel.value = county;
  sel.onchange = () => { county = sel.value; update(); };
  for (const b of $("f-cmp").querySelectorAll("button")) {
    b.onclick = () => { cmp = b.dataset.y; update(); };
  }
}

// ---- KPIs ----
function renderKpis() {
  const x = S(), w = x["w" + cmp] || {}, t = x["t" + cmp] || {};
  const wTot = ["same", "moved_open", "moved_closed", "other_county"].reduce((a, k) => a + (w[k] || 0), 0);
  const firstN = x.onestop - (x.first.returning || 0);
  const earlier = (t.earlier || 0) + (t.election_day || 0);
  const kpi = (v, k, hero) => h("div", { class: "kpi" + (hero ? " hero" : "") }, h("div", { class: "v" }, v), h("div", { class: "k" }, k));
  fill("kpi-sub", areaName());
  fill("kpis",
    kpi(fmtN(x.n), "ballots cast", true),
    kpi(fmtN(x.onestop), "one-stop (early, in person)"),
    kpi(fmtN(x.mail), "mail, e-mail or fax"),
    kpi(fmtP(pct(firstN, x.onestop)), "of one-stop voters are voting one-stop for the first time"),
    kpi(fmtP(pct(earlier, x.n)), `are voting earlier than they did in ${cmp}`),
    kpi(fmtP(pct(w.moved_closed || 0, wTot)), `of returning one-stop voters had a ${cmp} site that has since closed`));
}

// ---- daily cumulative lines ----
function series() {
  const out = {};
  for (const y of YEARS) {
    const src = y === "2026" ? S().daily : ((D.prior_daily[y] || {})[county] || {});
    let cum = 0; const pts = [];
    const last = y === "2026" ? evDay() : 3;
    for (let dbe = EV_FIRST; dbe >= 3; dbe--) {
      if (y === "2026" && (last == null || dbe < last)) break;
      cum += src[dbe] || 0;
      pts.push({ day: EV_FIRST - dbe + 1, dbe, n: src[dbe] || 0, cum });
    }
    out[y] = pts;
  }
  return out;
}
function renderDaily() {
  const ser = series(), box = $("daily");
  const W = Math.max(320, box.clientWidth || 700), H = 260, m = { l: 56, r: 64, t: 10, b: 30 };
  const maxY = Math.max(1, ...YEARS.flatMap(y => ser[y].map(p => p.cum)));
  const step = niceStep(maxY / 4), top = Math.ceil(maxY / step) * step;
  const X = d => m.l + (d - 1) / (EV_DAYS - 1) * (W - m.l - m.r);
  const Y = v => m.t + (1 - v / top) * (H - m.t - m.b);
  const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Cumulative one-stop ballots by day of early voting, ${areaName()}` });
  for (let v = 0; v <= top; v += step) {
    s("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: v ? "var(--grid)" : "var(--axis)", "stroke-width": 1 }, svg);
    s("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, svg).textContent = compact(v);
  }
  for (let d = 1; d <= EV_DAYS; d += (W < 560 ? 4 : 2)) s("text", { x: X(d), y: H - 10, "text-anchor": "middle" }, svg).textContent = `Day ${d}`;
  const ends = [];
  for (const y of YEARS) {
    const pts = ser[y]; if (!pts.length) continue;
    s("path", { d: pts.map((p, i) => `${i ? "L" : "M"}${X(p.day)},${Y(p.cum)}`).join(""), fill: "none",
      stroke: `var(--y${y})`, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    const e = pts[pts.length - 1];
    s("circle", { cx: X(e.day), cy: Y(e.cum), r: 4, fill: `var(--y${y})`, stroke: "var(--surface)", "stroke-width": 2 }, svg);
    ends.push({ y, py: Y(e.cum), x: X(e.day), v: e.cum });
  }
  // End labels: the year at each line's end, unless two would collide.
  ends.sort((a, b) => a.py - b.py);
  ends.forEach((e, i) => { if (i && e.py - ends[i - 1].py < 14 && !ends[i - 1].skip) { e.skip = true; return; }
    s("text", { x: e.x + 8, y: e.py + 4, class: "end" }, svg).textContent = e.y; });
  // Crosshair + tooltip listing every year at the hovered day.
  const cross = s("line", { y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" }, svg);
  const hit = s("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" }, svg);
  hit.addEventListener("pointermove", ev => {
    const r = svg.getBoundingClientRect(), d = Math.max(1, Math.min(EV_DAYS, Math.round(1 + (ev.clientX - r.left - m.l) / (W - m.l - m.r) * (EV_DAYS - 1))));
    cross.setAttribute("x1", X(d)); cross.setAttribute("x2", X(d)); cross.setAttribute("visibility", "visible");
    showTip(ev, `Day ${d} of early voting`, YEARS.slice().reverse().map(y => {
      const p = ser[y].find(q => q.day === d);
      return [`var(--y${y})`, p ? fmtN(p.cum) : "—", p ? `${y} total (${fmtDate(dateOf(y, p.dbe))}: ${fmtN(p.n)})` : `${y}: not yet`];
    }));
  });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  box.replaceChildren(svg);
  fill("daily-legend", ...YEARS.slice().reverse().map(y => h("span", {}, h("span", { class: "lk", style: { background: `var(--y${y})` } }), y)));
  const p26 = ser["2026"], last = p26[p26.length - 1];
  if (last) {
    const same = ser[cmp].find(p => p.day === last.day);
    fill("daily-sub", `Running total. Through day ${last.day}: ${fmtN(last.cum)} one-stop ballots`,
      same ? `, ${fmtP(pct(last.cum - same.cum, same.cum), 0).replace(/^(\d)/, "+$1")} vs. ${fmtN(same.cum)} at the same point in ${cmp}.` : ".");
  } else fill("daily-sub", "No one-stop ballots yet.");
  const rows = Array.from({ length: EV_DAYS }, (_, i) => i + 1).map(d => h("tr", {}, h("td", {}, `Day ${d}`),
    ...YEARS.map(y => { const p = ser[y].find(q => q.day === d); return h("td", { class: "num" }, p ? fmtN(p.n) : "—"); }),
    ...YEARS.map(y => { const p = ser[y].find(q => q.day === d); return h("td", { class: "num" }, p ? fmtN(p.cum) : "—"); })));
  fill("daily-table", h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Day"),
    ...YEARS.map(y => h("th", { class: "num" }, `${y} that day`)), ...YEARS.map(y => h("th", { class: "num" }, `${y} total`)))), h("tbody", {}, rows)));
}
function niceStep(raw) { const p = 10 ** Math.floor(Math.log10(raw || 1)), f = raw / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p; }
function compact(v) { return v >= 1e6 ? `${+(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${+(v / 1e3).toFixed(0)}K` : String(v); }

// ---- one-stop ballots by day, every federal general election ----
const HIST_FIRST = 20;
let histKind = "midterm", histMeasure = "day";
const isMidterm = y => (+y - 2002) % 4 === 0;
// Earlier elections share one hue, lighter the older they are; presidential years are dashed.
function histColor(y, years) {
  if (y === "2026") return "var(--y2026)";
  const prior = years.filter(v => v !== "2026"), i = prior.indexOf(y);
  const mix = prior.length > 1 ? 30 + 70 * i / (prior.length - 1) : 100;
  return `color-mix(in oklab, var(--y2024) ${Math.round(mix)}%, var(--surface))`;
}
function renderHist() {
  const all = [...Object.keys(D.prior_daily), "2026"].sort();
  const years = all.filter(y => y === "2026" || histKind === "all" || (histKind === "midterm") === isMidterm(y));
  const last26 = evDay(), cum = histMeasure === "cum";
  const ser = {};
  for (const y of years) {
    const src = y === "2026" ? S().daily : ((D.prior_daily[y] || {})[county] || {});
    const days = Object.keys(src).map(Number);
    if (!days.length) { ser[y] = []; continue; }
    // Start at the first day with real volume; a few ballots are dated before early voting opened.
    const peak = Math.max(...days.map(d => src[d])), pts = [];
    const first = Math.max(...days.filter(d => d <= HIST_FIRST && src[d] >= peak * 0.05));
    let run = 0;
    for (let dbe = first; dbe >= 3; dbe--) {
      if (y === "2026" && (last26 == null || dbe < last26)) break;
      run += src[dbe] || 0;
      pts.push({ dbe, n: src[dbe] || 0, cum: run });
    }
    ser[y] = pts;
  }
  const val = p => cum ? p.cum : p.n;
  for (const [id, cur, set] of [["hist-kind", histKind, v => { histKind = v; }], ["hist-measure", histMeasure, v => { histMeasure = v; }]])
    for (const b of $(id).querySelectorAll("button")) { b.classList.toggle("on", b.dataset.v === cur); b.onclick = () => { set(b.dataset.v); renderHist(); }; }
  fill("hist-sub", `${cum ? "Running total of" : "Daily"} one-stop ballots in ${areaName()} by days before Election Day, `
    + `${histKind === "midterm" ? "in every midterm election" : histKind === "pres" ? "in every presidential election, with 2026" : "in every federal general election"} since 2002.`);
  const box = $("hist");
  const W = Math.max(320, box.clientWidth || 700), H = 300, m = { l: 56, r: 48, t: 10, b: 30 };
  const maxY = Math.max(1, ...years.flatMap(y => ser[y].map(val)));
  const step = niceStep(maxY / 4), top = Math.ceil(maxY / step) * step;
  const X = dbe => m.l + (HIST_FIRST - dbe) / (HIST_FIRST - 3) * (W - m.l - m.r);
  const Y = v => m.t + (1 - v / top) * (H - m.t - m.b);
  const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `One-stop ballots by days before Election Day, ${years.join(", ")}, ${areaName()}` });
  for (let v = 0; v <= top; v += step) {
    s("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: v ? "var(--grid)" : "var(--axis)", "stroke-width": 1 }, svg);
    s("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, svg).textContent = compact(v);
  }
  for (let d = HIST_FIRST; d >= 3; d -= (W < 560 ? 4 : 2)) s("text", { x: X(d), y: H - 10, "text-anchor": "middle" }, svg).textContent = d === HIST_FIRST ? `${d} days before` : d;
  const ends = [];
  for (const y of years) {   // oldest first, so 2026 and recent years draw on top
    const pts = ser[y]; if (!pts.length) continue;
    const c = histColor(y, years);
    s("path", { d: pts.map((p, i) => `${i ? "L" : "M"}${X(p.dbe)},${Y(val(p))}`).join(""), fill: "none", stroke: c,
      "stroke-width": y === "2026" ? 3 : 2, "stroke-dasharray": y !== "2026" && !isMidterm(y) ? "5 3" : null,
      "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    const e = pts[pts.length - 1];
    if (y === "2026") s("circle", { cx: X(e.dbe), cy: Y(val(e)), r: 4, fill: c, stroke: "var(--surface)", "stroke-width": 2 }, svg);
    ends.push({ y, py: Y(val(e)), x: X(e.dbe) });
  }
  // Year labels at line ends, skipping any that would collide (2026 always wins).
  ends.sort((a, b) => (b.y === "2026") - (a.y === "2026") || a.py - b.py);
  const placed = [];
  for (const e of ends) {
    if (placed.some(p => Math.abs(p.py - e.py) < 13 && Math.abs(p.x - e.x) < 34)) continue;
    placed.push(e);
    s("text", { x: e.x + 6, y: e.py + 4, class: "end" }, svg).textContent = e.y;
  }
  const cross = s("line", { y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" }, svg);
  const hit = s("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" }, svg);
  hit.addEventListener("pointermove", ev => {
    const r = svg.getBoundingClientRect();
    const d = Math.max(3, Math.min(HIST_FIRST, Math.round(HIST_FIRST - (ev.clientX - r.left - m.l) / (W - m.l - m.r) * (HIST_FIRST - 3))));
    cross.setAttribute("x1", X(d)); cross.setAttribute("x2", X(d)); cross.setAttribute("visibility", "visible");
    showTip(ev, `${d} days before Election Day`, years.slice().reverse().map(y => {
      const p = ser[y].find(q => q.dbe === d);
      return [histColor(y, years), p ? fmtN(val(p)) : "—", p ? `${y} (${fmtDate(dateOf(y, d))})` : `${y}: no early voting${y === "2026" ? " yet" : ""}`];
    }));
  });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  box.replaceChildren(svg);
  fill("hist-legend", ...years.slice().reverse().map(y => h("span", {}, h("span", { class: "lk", style: {
    background: y === "2026" || isMidterm(y) ? histColor(y, years) : `repeating-linear-gradient(90deg, ${histColor(y, years)} 0 5px, transparent 5px 8px)` } }), y)));
  const rows = [];
  for (let d = HIST_FIRST; d >= 3; d--) rows.push(h("tr", {}, h("td", {}, `${d} days before`),
    ...years.map(y => { const p = ser[y].find(q => q.dbe === d); return h("td", { class: "num" }, p ? fmtN(val(p)) : "—"); })));
  fill("hist-table", h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Day"), ...years.map(y => h("th", { class: "num" }, y)))), h("tbody", {}, rows)));
}

// ---- horizontal bars (single series) ----
function hbars(title, items, max) {
  max = max || Math.max(1e-9, ...items.map(i => i.v));
  return h("div", {}, h("h3", {}, title), h("div", { class: "hbar" }, items.flatMap(i => {
    const b = h("div", { class: "b", style: { width: `${Math.max(0.5, 100 * i.v / max)}%` } });
    const t = h("div", { class: "t" }, b, h("span", { class: "n" }, i.label));
    tipOn(t, i.name, [[null, i.label, i.note || ""]]);
    return [h("div", { class: "l", title: i.name }, i.name), t];
  })));
}
function renderFirst() {
  const x = S(), f = x.first, firstN = x.onestop - (f.returning || 0), beforeN = (f.mail || 0) + (f.election_day || 0);
  fill("first-sub", `${fmtN(firstN)} of ${fmtN(x.onestop)} one-stop voters in ${areaName()} (${fmtP(pct(firstN, x.onestop))}) have no earlier one-stop ballot on record:`
    + " none in a federal general election since 2002, or in any NC election (including primaries and municipal elections) since 2016.");
  const kinds = [["new", "No earlier NC vote on record"], ["mail", "Voted before, by mail"], ["election_day", "Voted before, on Election Day"]];
  const ages = Object.entries(x.first_age).filter(([a]) => a !== "unknown")
    .map(([a, v]) => ({ name: a, v: v[0], label: `${fmtN(v[0])} (${fmtP(pct(v[0], beforeN), 0)})`, note: `${fmtP(pct(v[0], v[1]))} of one-stop voters aged ${a}` }));
  fill("first",
    hbars("Their voting history", kinds.map(([k, n]) => ({ name: n, v: f[k] || 0, label: `${fmtN(f[k] || 0)} (${fmtP(pct(f[k] || 0, firstN), 0)})` }))),
    hbars(`Voted in NC before, first time one-stop (${fmtN(beforeN)}), by age`, ages));
}

// ---- 100% stacked bars ----
function stack(parts, total) {
  const row = h("div", { class: "stack" });
  for (const p of parts) {
    if (!p.n) continue;
    const share = 100 * p.n / total;
    const seg = h("div", { style: { width: `${share}%`, background: p.color } });
    row.append(seg);
    tipOn(seg, p.name, [[p.color, fmtP(share), `${fmtN(p.n)} voters`]]);
    seg.dataset.label = fmtP(share, 0);
  }
  requestAnimationFrame(() => row.querySelectorAll("div").forEach(seg => {  // label only segments it fits in
    const t = h("span", {}, seg.dataset.label);
    seg.replaceChildren(t);
    if (t.offsetWidth + 12 > seg.clientWidth) seg.replaceChildren();
    else seg.style.color = inkOn(getComputedStyle(seg).backgroundColor);
  }));
  return row;
}
// Black or white text, whichever contrasts more with an rgb() background.
function inkOn(rgb) {
  const [r, g, b] = (rgb.match(/[\d.]+/g) || [0, 0, 0]).slice(0, 3).map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
  const L = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return (L + 0.05) / 0.05 > 1.05 / (L + 0.05) ? "#0b0b0b" : "#ffffff";
}
const legend = (id, parts) => fill(id, parts.map(p => h("span", {}, h("span", { class: "sw", style: { background: p.color } }), p.name)));
const TIMING = [
  { k: "election_day", name: "Voted on Election Day then", color: "var(--ed)" },
  { k: "earlier", name: "Earlier in early voting", color: "var(--earlier)" },
  { k: "same", name: "Same day of early voting", color: "var(--mid)" },
  { k: "later", name: "Later", color: "var(--later)" },
];
const USUAL = [
  { k: "election_day", name: "Voted on Election Day in 2022 or 2024", color: "var(--ed)" },
  { k: "earlier", name: "Earlier than before", color: "var(--earlier)" },
  { k: "same", name: "About the same time as before", color: "var(--mid)" },
  { k: "later", name: "Later than before", color: "var(--later)" },
  { k: "none", name: "No one-stop or Election Day vote in 2022 or 2024", color: "var(--county)" },
];
function renderTiming() {
  const x = S(), u = x.usual || {}, parts = USUAL.map(p => ({ ...p, n: u[p.k] || 0 }));
  const uTot = parts.reduce((a, p) => a + p.n, 0);
  fill("usual-sub", `${fmtN(uTot)} one-stop voters in ${areaName()} so far, by when they voted compared with 2022 and 2024.`
    + " \"Before\" is the earlier election in which they voted closest to Election Day; \"about the same time\" is the same number of days before Election Day (and so the same weekday)."
    + " Anyone who voted on Election Day in either election is counted as an Election Day voter.");
  legend("usual-legend", USUAL);
  fill("usual", uTot ? [h("div", { class: "stack-row" }, h("div", { class: "y" }, "2026"), stack(parts, uTot)),
    h("div", { class: "kpis" }, parts.map(p => h("div", { class: "kpi" }, h("div", { class: "v" }, fmtP(pct(p.n, uTot), 0)),
      h("div", { class: "k" }, `${p.name} (${fmtN(p.n)})`))))] : h("p", { class: "empty" }, "No one-stop ballots yet."));
  fill("timing-sub", `All 2026 voters in ${areaName()}, including mail, who also voted in each earlier election, by when they voted then compared with now.`);
  legend("timing-legend", TIMING);
  fill("timing", ["2024", "2022"].map(y => {
    const t = x["t" + y] || {}, parts = TIMING.map(p => ({ ...p, n: t[p.k] || 0 }));
    const tot = parts.reduce((a, p) => a + p.n, 0);
    return h("div", {}, h("div", { class: "stack-row", style: { opacity: y === cmp ? 1 : .55 } }, h("div", { class: "y" }, y), tot ? stack(parts, tot) : h("div", { class: "empty" }, "No data")),
      h("p", { class: "foot", style: { margin: "0 0 8px 74px" } }, `${fmtP(pct(t.none || 0, x.n))} of 2026 voters so far didn't vote in ${y}.`));
  }));
  const dbe = evDay(), day = dbe == null ? null : EV_FIRST - dbe + 1;
  fill("timing-foot", day && day < EV_DAYS
    ? `Early voting is on day ${day} of ${EV_DAYS}, so everyone counted so far is voting early in the period; these shares will move toward "later" as the period goes on.`
    : "");
}
const WHERE = [
  { k: "same", name: "Same site", color: "var(--same)" },
  { k: "moved_open", name: "Different site (old site still open)", color: "var(--open)" },
  { k: "moved_closed", name: "Different site (old site closed)", color: "var(--closed)" },
  { k: "other_county", name: "Different county", color: "var(--county)" },
];
function renderWhere() {
  const x = S();
  fill("where-sub", `2026 one-stop voters in ${areaName()} compared with where they voted one-stop before.`);
  legend("where-legend", WHERE);
  fill("where", ["2024", "2022"].map(y => {
    const w = x["w" + y] || {}, parts = WHERE.map(p => ({ ...p, n: w[p.k] || 0 }));
    const tot = parts.reduce((a, p) => a + p.n, 0);
    return h("div", { class: "stack-row", style: { opacity: y === cmp ? 1 : .55 } }, h("div", { class: "y" }, y),
      tot ? stack(parts, tot) : h("div", { class: "empty" }, "No data"));
  }));
}

// Sortable table: cols [{k, name, num, sort}], rows [{sort: {k: value}, group?, tr: () => <tr>}].
// With groupRow(group, rows) -> <tr>, rows are kept together by group (in alphabetical order),
// each under its header row, and sorting applies within each group.
function sortableTable(cols, rows, initial, groupRow) {
  let key = initial.key, dir = initial.dir;
  const wrap = h("div");
  const draw = () => {
    const sorted = rows.slice().sort((a, b) => { const va = a.sort[key], vb = b.sort[key];
      return (groupRow ? a.group.localeCompare(b.group) : 0)
        || (typeof va === "string" ? va.localeCompare(vb) : (va ?? -1) - (vb ?? -1)) * dir; });
    const body = groupRow ? sorted.flatMap((r, i) => i && sorted[i - 1].group === r.group ? [r.tr()]
      : [groupRow(r.group, sorted.filter(o => o.group === r.group)), r.tr()]) : sorted.map(r => r.tr());
    wrap.replaceChildren(h("table", {}, h("thead", {}, h("tr", {}, cols.map(c => h("th", {
      class: [c.num ? "num" : "", c.sort ? "sortable" : ""].join(" "), "aria-sort": c.k === key ? (dir > 0 ? "ascending" : "descending") : null,
      onclick: c.sort ? () => { if (key === c.k) dir = -dir; else { key = c.k; dir = c.num ? -1 : 1; } draw(); } : null
    }, c.name, c.k === key ? (dir > 0 ? " ▲" : " ▼") : "")))), h("tbody", {}, body)));
  };
  draw();
  return wrap;
}

// ---- closed sites: where each closed earlier site's voters are voting now ----
function milesBetween(a, b) {
  const p = D.coords[a], q = D.coords[b];
  if (!p || !q) return null;
  const r = Math.PI / 180, dLat = (q[0] - p[0]) * r, dLon = (q[1] - p[1]) * r;
  const x = Math.sin(dLat / 2) ** 2 + Math.cos(p[0] * r) * Math.cos(q[0] * r) * Math.sin(dLon / 2) ** 2;
  return 3958.8 * 2 * Math.asin(Math.sqrt(x));
}
const fmtMi = m => m == null ? "" : ` (${m < 10 ? m.toFixed(1) : Math.round(m)} mi)`;
// Voter-weighted totals over closed-site rows: voting earlier, distance, and reported results.
function flowTotals(rs) {
  const a = { n: 0, earlier: 0, timed: 0, miles: 0, located: 0, DEM: 0, REP: 0, OTHER: 0, reported: 0 };
  for (const r of rs) for (const k in a) a[k] += r.t[k];
  return a;
}
const resultText = t => { const v = t.DEM + t.REP + t.OTHER; return v ? `D ${fmtP(pct(t.DEM, v), 0)} · R ${fmtP(pct(t.REP, v), 0)}` : "—"; };
function resultBar(t, title) {
  const v = t.DEM + t.REP + t.OTHER;
  if (!v) return h("span", { class: "addr" }, "Not reported by site");
  const parts = [[t.DEM, "var(--dem)", "Democrat"], [t.REP, "var(--rep)", "Republican"], [t.OTHER, "var(--mid)", "Other"]].filter(([n]) => n);
  const mini = h("div", { class: "mini" }, parts.map(([n, c]) => h("div", { style: { width: `${100 * n / v}%`, background: c } })));
  tipOn(mini, title, parts.map(([n, c, name]) => [c, fmtP(pct(n, v)), `${name} (${fmtN(n)})`]));
  return h("div", {}, mini, h("div", { class: "addr" }, resultText(t)));
}
function renderFlows() {
  const flows = D.flows[cmp] || {}, timing = D.flow_timing[cmp] || {}, results = D.results[cmp] || {}, contest = D.result_contests[cmp];
  const ids = Object.keys(flows).filter(id => inArea(id) && !openIn(id, "2026"));
  fill("flows-sub", `${cmp} one-stop sites in ${areaName()} that aren't 2026 sites, and how their ${cmp} voters who have cast a 2026 ballot so far are voting.`
    + ` "Voting earlier" is the share casting their 2026 ballot earlier in early voting than they did at that site in ${cmp}.`
    + " Distances are straight-line miles from the closed site to the 2026 site each voter used.");
  fill("flows-foot", `${cmp} result: the ${contest} race among one-stop ballots cast at the site, from the State Board's precinct results.`
    + ` Only some counties report early votes by site; the rest count them in each voter's home precinct, so those sites show none.`
    + " Years open: 2022 and later are matched by street address; 2014–2020 by site name.");
  const parts = [{ name: "Another 2026 site", color: "var(--bar)" }, { name: "By mail", color: "var(--county)" },
    { name: `Democrat (${cmp})`, color: "var(--dem)" }, { name: `Republican (${cmp})`, color: "var(--rep)" }];
  legend("flows-legend", parts);
  if (!ids.length) { fill("flows-kpis"); fill("flows", h("p", { class: "empty" }, `No ${cmp} sites closed in ${areaName()}.`)); return; }
  const rows = ids.map(id => {
    const d = flows[id], tot = Object.values(d).reduce((a, b) => a + b, 0);
    const mail = d.MAIL || 0, other = tot - mail;
    const t = timing[id] || {}, tTot = (t.earlier || 0) + (t.same || 0) + (t.later || 0);
    const earlier = pct(t.earlier || 0, tTot);
    let miles = 0, located = 0;
    for (const [k, n] of Object.entries(d)) { const m = k === "MAIL" || k === "UNMATCHED" ? null : milesBetween(id, k); if (m != null) { miles += m * n; located += n; } }
    const res = results[id] || { DEM: 0, REP: 0, OTHER: 0 };
    const years = [...new Set([...(D.older_years[id] || []), ...D.sites[id].years.filter(y => y !== "2026")])].sort();
    const top = Object.entries(d).filter(([k]) => k !== "MAIL").sort((a, b) => b[1] - a[1]).slice(0, 3);
    const avgMi = located ? miles / located : null, rv = res.DEM + res.REP + res.OTHER;
    return { group: titleCase(D.sites[id].county),
      t: { n: tot, earlier: t.earlier || 0, timed: tTot, miles, located, ...res, reported: rv ? 1 : 0 },
      sort: { site: siteName(id), n: tot, earlier: earlier ?? -1, miles: avgMi ?? -1, dem: rv ? pct(res.DEM - res.REP, rv) : -999 },
      tr: () => {
        const mini = h("div", { class: "mini" }, [[other, parts[0]], [mail, parts[1]]].filter(([n]) => n)
          .map(([n, p]) => h("div", { style: { width: `${100 * n / tot}%`, background: p.color } })));
        tipOn(mini, `${siteName(id)}: ${fmtN(tot)} ${cmp} voters have voted in 2026`,
          [[parts[0].color, fmtP(pct(other, tot)), "at a 2026 site"], [parts[1].color, fmtP(pct(mail, tot)), "by mail"]]);
        const early = h("td", { class: "num" }, fmtP(earlier, 0));
        tipOn(early, `${siteName(id)} voters vs. ${cmp}`, [[null, fmtP(earlier), `earlier (${fmtN(t.earlier || 0)})`],
          [null, fmtP(pct(t.same || 0, tTot)), `same day (${fmtN(t.same || 0)})`], [null, fmtP(pct(t.later || 0, tTot)), `later (${fmtN(t.later || 0)})`]]);
        return h("tr", {},
          h("td", {}, siteName(id), h("div", { class: "addr" }, siteAddr(id)), h("div", { class: "addr" }, `Open ${years.join(", ")}`)),
          h("td", {}, mini, h("div", { class: "addr" }, `${fmtN(tot)} voters`)),
          early,
          h("td", { class: "num" }, avgMi == null ? "—" : `${avgMi.toFixed(1)} mi`),
          h("td", {}, resultBar(res, `${siteName(id)}: ${cmp} ${contest}, one-stop ballots`)),
          h("td", { class: "dest" }, top.length ? top.map(([k, n]) => k === "UNMATCHED" ? `Unmatched site ${fmtP(pct(n, tot), 0)}`
            : `${siteName(k)}${fmtMi(milesBetween(id, k))} ${fmtP(pct(n, tot), 0)}`).join(" · ") : "—"));
      } };
  });
  const all = flowTotals(rows), avgMi = a => a.located ? `${(a.miles / a.located).toFixed(1)} mi` : "—";
  const repCounties = new Set(rows.filter(r => r.t.reported).map(r => r.group)).size;
  const kpi = (v, k) => h("div", { class: "kpi" }, h("div", { class: "v" }, v), h("div", { class: "k" }, k));
  fill("flows-kpis",
    kpi(fmtN(rows.length), `closed ${cmp} ${rows.length === 1 ? "site" : "sites"}, ${fmtN(all.n)} of their voters have voted in 2026`),
    kpi(fmtP(pct(all.earlier, all.timed), 0), "of those voters are voting earlier than they did in " + cmp),
    kpi(avgMi(all), "average distance to the 2026 site they used"),
    kpi(resultText(all), all.reported
      ? `${cmp} ${contest} at the ${all.reported} closed ${all.reported === 1 ? "site" : "sites"} reported by site (${repCounties} ${repCounties === 1 ? "county" : "counties"})`
      : `${cmp} result: no closed sites here are reported by site`));
  fill("flows", sortableTable([
    { k: "site", name: `Closed ${cmp} site`, sort: true }, { k: "n", name: "How they're voting", sort: true, num: false },
    { k: "earlier", name: "Voting earlier", num: true, sort: true }, { k: "miles", name: "Avg. distance", num: true, sort: true },
    { k: "dem", name: `${cmp} result`, sort: true }, { k: "dest", name: "2026 site" },
  ], rows, { key: "n", dir: -1 }, (name, rs) => {
    const a = flowTotals(rs);
    return h("tr", { class: "group" }, h("td", { colspan: 6 }, `${name} County`,
      h("span", { class: "n" }, [`${rs.length} closed ${rs.length === 1 ? "site" : "sites"}`, `${fmtN(a.n)} voters`,
        `${fmtP(pct(a.earlier, a.timed), 0)} voting earlier`, `avg. ${avgMi(a)}`,
        a.reported ? `${cmp}: ${resultText(a)}` : null].filter(Boolean).join(" · "))));
  }));
}

// ---- 2026 sites ----
function renderSites() {
  const ids = Object.keys(D.sites26).filter(inArea);
  fill("sites-sub", `One-stop ballots at each 2026 site in ${areaName()}, and where those voters voted one-stop in ${cmp}. Shares are of each site's ballots.`
    + (county === "NC" ? " Statewide shows every site; select a column heading to sort." : ""));
  if (!ids.length) { fill("sites", h("p", { class: "empty" }, "No 2026 one-stop sites with ballots yet.")); return; }
  const rows = ids.map(id => {
    const t = D.sites26[id], n = t.n, g = k => t[`${cmp}_${k}`] || 0;
    const isNew = !openIn(id, cmp);
    const r = { site: siteName(id), n, first: pct(t.first, n), same: pct(g("same"), n), closed: pct(g("moved_closed"), n), open: pct(g("moved_open"), n) };
    return { sort: r, tr: () => h("tr", {},
      h("td", {}, siteName(id), isNew ? h("span", { class: "badge" }, `new since ${cmp}`) : null, h("div", { class: "addr" }, siteAddr(id), county === "NC" ? ` · ${titleCase(D.sites[id].county)}` : "")),
      h("td", { class: "num" }, fmtN(n)), h("td", { class: "num" }, fmtP(r.first, 0)), h("td", { class: "num" }, fmtP(r.same, 0)),
      h("td", { class: "num" }, fmtP(r.closed, 0)), h("td", { class: "num" }, fmtP(r.open, 0))) };
  });
  fill("sites", sortableTable([
    { k: "site", name: "2026 site", sort: true }, { k: "n", name: "One-stop ballots", num: true, sort: true },
    { k: "first", name: "First-time one-stop", num: true, sort: true }, { k: "same", name: `Same site as ${cmp}`, num: true, sort: true },
    { k: "closed", name: `From a closed ${cmp} site`, num: true, sort: true }, { k: "open", name: `From another open ${cmp} site`, num: true, sort: true },
  ], rows, { key: "n", dir: -1 }));
}

// ---- site changes ----
function renderChanges() {
  const ch = D.changes[cmp] || {};
  fill("h-changes", `One-stop sites added and removed since ${cmp}`);
  const counties = county === "NC" ? Object.keys(ch) : [county].filter(c => ch[c]);
  const tot = k => counties.reduce((a, c) => a + ch[c][k].length, 0);
  fill("changes-sub", `${areaName()}: ${tot("added")} added, ${tot("removed")} removed, ${tot("renamed")} renamed (same address, new name).`
    + (county === "NC" ? " Choose a county to list its sites." : ""));
  if (county === "NC") {
    fill("changes"); return;
  }
  if (!counties.length) { fill("changes", h("p", { class: "empty" }, `No site changes since ${cmp}.`)); return; }
  const c = ch[county];
  const list = (title, ids) => h("div", {}, h("h3", {}, `${title} (${ids.length})`), ids.length
    ? h("ul", {}, ids.map(id => h("li", {}, siteName(id), h("div", { class: "addr" }, siteAddr(id))))) : h("p", { class: "empty" }, "None"));
  fill("changes", list("Added", c.added), list("Removed", c.removed), list("Renamed", c.renamed));
}

// ---- participation ----
function renderPart() {
  const x = S(), tot = Object.values(x.part).reduce((a, b) => a + b, 0);
  fill("part", hbars("", ["0", "1", "2", "3", "4", "5"].map(k => ({ name: `${k} of 5`, v: x.part[k] || 0,
    label: `${fmtP(pct(x.part[k] || 0, tot), 1)} (${fmtN(x.part[k] || 0)})` }))));
}

// ---- counties ----
function renderCounties() {
  const ch = D.changes[cmp] || {};
  const sitesIn = (c, y) => Object.values(D.sites).filter(s => s.county === c && s.years.includes(y)).length;
  const counts = {};
  for (const [id, s] of Object.entries(D.sites)) for (const y of s.years) { const k = `${s.county}|${y}`; counts[k] = (counts[k] || 0) + 1; }
  const rows = Object.keys(D.scopes).filter(k => k !== "NC").map(c => {
    const x = D.scopes[c], w = x["w" + cmp] || {}, t = x["t" + cmp] || {};
    const wTot = ["same", "moved_open", "moved_closed", "other_county"].reduce((a, k) => a + (w[k] || 0), 0);
    const r = { county: c, n: x.n, sitesThen: counts[`${c}|${cmp}`] || 0, sitesNow: counts[`${c}|2026`] || 0,
      added: ch[c] ? ch[c].added.length : 0, removed: ch[c] ? ch[c].removed.length : 0,
      first: pct(x.onestop - (x.first.returning || 0), x.onestop), earlier: pct((t.earlier || 0) + (t.election_day || 0), x.n),
      closed: pct(w.moved_closed || 0, wTot) };
    return { sort: r, tr: () => h("tr", { class: "pick" + (c === county ? " sel" : ""), tabindex: 0,
        onclick: () => { county = c; update(); scrollTo({ top: 0, behavior: "smooth" }); },
        onkeydown: ev => { if (ev.key === "Enter") { county = c; update(); } } },
      h("td", {}, titleCase(c)), h("td", { class: "num" }, fmtN(r.n)),
      h("td", { class: "num" }, `${r.sitesThen} → ${r.sitesNow}`), h("td", { class: "num" }, `+${r.added} / −${r.removed}`),
      h("td", { class: "num" }, fmtP(r.first, 0)), h("td", { class: "num" }, fmtP(r.earlier, 0)), h("td", { class: "num" }, fmtP(r.closed, 0))) };
  });
  fill("counties", sortableTable([
    { k: "county", name: "County", sort: true }, { k: "n", name: "Ballots", num: true, sort: true },
    { k: "sitesNow", name: `Sites ${cmp} → 2026`, num: true, sort: true }, { k: "removed", name: "Added / removed", num: true, sort: true },
    { k: "first", name: "First-time one-stop", num: true, sort: true }, { k: "earlier", name: `Earlier than ${cmp}`, num: true, sort: true },
    { k: "closed", name: `Returning voters whose ${cmp} site closed`, num: true, sort: true },
  ], rows, { key: "n", dir: -1 }));
}

function update() {
  writeHash();
  $("f-county").value = county;
  for (const b of $("f-cmp").querySelectorAll("button")) { b.classList.toggle("on", b.dataset.y === cmp); b.setAttribute("aria-pressed", b.dataset.y === cmp); }
  renderKpis(); renderDaily(); renderHist(); renderFirst(); renderTiming(); renderWhere();
  renderFlows(); renderSites(); renderChanges(); renderPart(); renderCounties();
}
readHash();
renderHeader(); renderFilters(); update();
let rz; addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(renderDaily, 150); });
addEventListener("hashchange", () => { readHash(); update(); });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
