#!/usr/bin/env python3
"""Match the State Board's early voting result codes to one-stop sites.

Most counties fold early votes into each voter's home precinct, but some report them
by early voting site, as "precincts" that aren't real ones (Real Precinct = N) with
codes like "EV-BOE" or "OS KDH". This matches those codes to the sites in
onestop_site_crosswalk.json, mainly by vote count (a site's top-of-ticket votes run
just under its one-stop ballot count) and then by name, and writes each matched
site's top-of-ticket result by party.

  ./build_site_results.py      # -> ncsbe_cache/onestop_site_results.json, listing matches to check

Downloads results_pct_<date>.zip into ncsbe_cache/ if it isn't there.
"""
import collections
import csv
import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "ncsbe_cache"
BASE = "https://s3.amazonaws.com/dl.ncsbe.gov/ENRS"
# Election -> the top-of-ticket contest every voter had on the ballot.
CONTESTS = {"20221108": "US SENATE", "20241105": "US PRESIDENT"}
NOT_A_SITE = re.compile(r"^(ABS|PROV|TRANS|CURBSIDE)|ABSENTEE|PROVI|PROVSI|TRANSFER|MAIL|\bSUP")
RANGE = re.compile(r"\s+\d+-\d+$")             # Wake 2022: "OSLL 1-40", "OSLL 41-80", ...
PREFIX = re.compile(r"^(EARLY VOTING|ONE STOP|EV|OS)[\s-]*|[\s-]*(EV|OS)$")
STOP = {"THE", "OF", "AND", "AT", "COUNTY", "CENTER", "COMMUNITY", "BUILDING", "BLDG", "OFFICE", "OFFICES"}
# A site's top-of-ticket votes per one-stop ballot; codes outside this range aren't trusted.
RATIO = (0.97, 1.03)


def fetch(el):
    path = CACHE / f"results_pct_{el}.zip"
    if not path.exists():
        d = f"{el[:4]}_{el[4:6]}_{el[6:]}"
        print(f"downloading {path.name}", file=sys.stderr)
        urllib.request.urlretrieve(f"{BASE}/{d}/{path.name}", path)
    return path


def code_totals(el):
    """{county: {code: Counter(party -> votes)}} for early voting site codes."""
    z = zipfile.ZipFile(fetch(el))
    out = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    with io.TextIOWrapper(z.open(z.namelist()[0]), encoding="latin-1") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            if r["Real Precinct"] != "N" or r["Contest Name"] != CONTESTS[el]:
                continue
            code = RANGE.sub("", r["Precinct"].strip())
            if NOT_A_SITE.search(code):
                continue
            party = r["Choice Party"].strip()
            out[r["County"]][code][party if party in ("DEM", "REP") else "OTHER"] += int(r["Total Votes"])
    return out


def name_score(code, site_name):
    """How well a result code names a site: 1 for a clear match, 0 for none."""
    c = PREFIX.sub("", code).strip()
    words = [w for w in re.findall(r"[A-Z0-9]+", site_name.replace("'", "")) if w not in STOP]
    m = re.match(r"^([A-Z]{2}) - ", site_name)     # Wake: "LL - LAKE LYNN COMMUNITY CENTER" <-> "EVLL"
    if m:
        return 1.0 if c == m.group(1) else 0.0
    if not c or c.isdigit():
        return 0.0
    if "BOARD" in site_name and c in ("BOE", "LOB", "OFFICE", "ADMIN"):
        return 0.8
    toks = re.findall(r"[A-Z0-9]+", c)
    if all(any(w.startswith(t) or t.startswith(w) for w in words) for t in toks):
        return 1.0
    initials = "".join(w[0] for w in words)
    if c.replace(" ", "") in initials or c.replace(" ", "") in "".join(w[:2] for w in words):
        return 0.6
    return 0.0


def match_county(codes, sites):
    """Pair codes with sites, best pairs first. sites: [(id, name, ballots)]."""
    pairs = []
    for code, v in codes.items():
        n = sum(v.values())
        for sid, name, ballots in sites:
            ratio = n / ballots if ballots else 0
            if RATIO[0] <= ratio <= RATIO[1]:
                pairs.append((abs(ratio - 0.995) * 100 - 2 * name_score(code, name), code, sid))
    pairs.sort()
    used_c, used_s, out = set(), set(), {}
    for _, code, sid in pairs:
        if code not in used_c and sid not in used_s:
            used_c.add(code), used_s.add(sid)
            out[code] = sid
    return out


def main(verbose=True):
    xw = json.loads((CACHE / "onestop_site_crosswalk.json").read_text())["sites"]
    result = {"description": "Top-of-ticket early voting results by one-stop site, for counties that report by site.",
              "contests": CONTESTS, "elections": {}}
    for el in CONTESTS:
        out, counties = {}, 0
        for county, codes in sorted(code_totals(el).items()):
            codes = {c: v for c, v in codes.items() if sum(v.values())}
            if len(codes) < 2:          # one countywide early voting total: not by site
                continue
            sites = [(s["id"], s["elections"][el]["site_name"], s["elections"][el]["one_stop_ballots"])
                     for s in xw if s["county"] == county and el in s["elections"]]
            m = match_county(codes, sites)
            counties += 1
            for code in sorted(set(codes) - set(m)) if verbose else ():
                print(f"{el} {county}: no site for code {code!r} ({sum(codes[code].values())} votes)", file=sys.stderr)
            for code, sid in m.items():
                v = codes[code]
                out[sid] = {"code": code, "DEM": v["DEM"], "REP": v["REP"], "OTHER": v["OTHER"]}
                name = next(n for i, n, _ in sites if i == sid)
                if verbose and name_score(code, name) == 0:
                    print(f"{el} {county}: {code!r} -> {name} (by vote count only)", file=sys.stderr)
        result["elections"][el] = out
        print(f"{el}: {len(out)} sites in {counties} counties", file=sys.stderr)
    path = CACHE / "onestop_site_results.json"
    path.write_text(json.dumps(result, indent=1))
    print(f"wrote {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
