#!/usr/bin/env python3
"""Characterize 2026 general-election ballots cast so far by each voter's history.

Inputs (ncsbe_cache/):
  absentee_20261103.zip         2026 ballots so far (one-stop + mail), with demographics
  ballots_federal.tsv.gz        normalized 2002-2024 ballots (build_ballot_history.py)
  ncvhis_Statewide.zip          voter history, every NC election since 2016 (adds Election Day, primary and municipal votes)
  onestop_site_crosswalk.json   physical-site ids (build_site_crosswalk.py)

Output: aggregates only (statewide and per county), never individual voters.
Run directly to write ncsbe_cache/analysis_2026_voters.json.

Timing compares days before Election Day. Early voting opened 19 days out in
2022 (Oct 20), 2024 (Oct 17) and 2026 (Oct 15), so day N of early voting lines
up across years.
"""

import collections
import csv
import datetime as dt
import gzip
import json
import sys

from build_ballot_history import CACHE, ELECTIONS, counted, parse_date, zip_rows

E26 = "20261103"
EDAY26 = dt.date(2026, 11, 3)
COMPARE = ["20241105", "20221108"]
LAST5 = ["20161108", "20181106", "20201103", "20221108", "20241105"]
HIST_ONESTOP = {
    "ABSENTEE ONESTOP",
    "ABSENTEE CURBSIDE",
    "EARLY VOTING IN-PERSON",
    "EARLY VOTING CURBSIDE",
}
HIST_ELECTION_DAY = {
    "ELECTION DAY IN-PERSON",
    "ELECTION DAY CURBSIDE",
    "PROVISIONAL",
    "TRANSFER",
}
AGES = ["18-25", "26-40", "41-65", "66+"]
# Early voting runs from 19 to 3 days before Election Day.
EV_FIRST, EV_LAST = 19, 3


def age_band(a):
    try:
        a = int(a)
    except ValueError:
        return "unknown"
    return "18-25" if a < 26 else "26-40" if a < 41 else "41-65" if a < 66 else "66+"


def load_crosswalk():
    """Return ({(election, county, site_name): site id}, {site id: info})."""
    x = json.load(open(CACHE / "onestop_site_crosswalk.json"))
    ids, info = {}, {}
    for s in x["sites"]:
        latest = s["elections"][max(s["elections"])]
        info[s["id"]] = {
            "county": s["county"],
            "name": latest["site_name"],
            "address": s["address"],
            "years": sorted(e[:4] for e in s["elections"]),
            "ballots": {e[:4]: v["one_stop_ballots"] for e, v in s["elections"].items()},
        }
        for e, v in s["elections"].items():
            ids[(e, s["county"], v["site_name"])] = s["id"]
    return ids, info


def load_2026(sid):
    voters = {}
    latest = None
    for r in zip_rows(CACHE / f"absentee_{E26}.zip"):
        if not counted(r, E26) or not r["ncid"]:
            continue
        d = parse_date(r["ballot_rtn_dt"])
        onestop = r["ballot_req_type"] == "EARLY VOTING"
        if onestop and d:
            latest = max(latest or d, d)
        voters[r["ncid"]] = {
            "county": r["county_desc"],
            "method": "ONESTOP" if onestop else "MAIL",
            "dbe": (EDAY26 - d).days if d else None,
            "site": sid.get((E26, r["county_desc"], r["site_name"])) if onestop else None,
            "site_name": r["site_name"] if onestop else "",
            "age": age_band(r["age"]),
        }
    return voters, latest


def load_prior(ncids):
    """ncid -> election -> (method, days_before, county, site_name), 2002-2024."""
    prior = collections.defaultdict(dict)
    with gzip.open(CACHE / "ballots_federal.tsv.gz", "rt") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            if r["election"] != E26 and r["ncid"] in ncids:
                prior[r["ncid"]][r["election"]] = (
                    r["method"],
                    int(r["days_before"]) if r["days_before"] else None,
                    r["county"],
                    r["site_name"],
                )
    return prior


def load_hist(ncids):
    """ncid -> election date (YYYYMMDD) -> voting_method, for every NC election before
    this one in the voter history file (2016 on: generals, primaries, municipal)."""
    hist = collections.defaultdict(dict)
    for r in zip_rows(CACHE / "ncvhis_Statewide.zip", "\t"):
        if r["ncid"] in ncids and r["voting_method"] != "ELIGIBLE DID NOT VOTE":
            m, d, y = r["election_lbl"].split("/")
            if f"{y}{m}{d}" < E26:
                hist[r["ncid"]][f"{y}{m}{d}"] = r["voting_method"]
    return hist


def classify(v, p, h, sid, open26):
    """Per-voter categories (kept in memory only; the report gets aggregates)."""
    c = {}
    if v["method"] == "ONESTOP":
        had_onestop = any(m == "ONESTOP" for m, *_ in p.values()) or any(
            m in HIST_ONESTOP for m in h.values()
        )
        if had_onestop:
            c["first"] = None
        elif any(m == "MAIL" for m, *_ in p.values()):
            c["first"] = "mail"
        elif any(m in HIST_ELECTION_DAY for m in h.values()):
            c["first"] = "election_day"
        elif h:
            c["first"] = "mail"
        else:
            c["first"] = "new"
    for e in COMPARE:
        y = e[:4]
        pe, he = p.get(e), h.get(e)
        if pe and pe[1] is not None and v["dbe"] is not None:
            c[f"t{y}"] = (
                "earlier" if v["dbe"] > pe[1] else "same" if v["dbe"] == pe[1] else "later"
            )
        elif he in HIST_ELECTION_DAY:
            c[f"t{y}"] = "election_day"
        elif he:
            c[f"t{y}"] = "unknown"
        else:
            c[f"t{y}"] = "none"
        # Where: one-stop voters both years, compared by physical site.
        if v["method"] == "ONESTOP" and pe and pe[0] == "ONESTOP":
            there = sid.get((e, pe[2], pe[3]))
            c[f"from{y}"] = there
            if pe[2] != v["county"]:
                c[f"w{y}"] = "other_county"
            elif not (there and v["site"]):
                c[f"w{y}"] = "unmatched"
            elif there == v["site"]:
                c[f"w{y}"] = "same"
            else:
                c[f"w{y}"] = "moved_open" if there in open26 else "moved_closed"
        elif pe and pe[0] == "ONESTOP":
            c[f"from{y}"] = sid.get((e, pe[2], pe[3]))  # now voting by mail
    # Usual timing: 2026 one-stop voters against their Election Day votes, or their latest
    # one-stop vote (fewest days before Election Day), in the comparison elections.
    if v["method"] == "ONESTOP":
        days = [p[e][1] for e in COMPARE if p.get(e) and p[e][0] == "ONESTOP" and p[e][1] is not None]
        if any(h.get(e) in HIST_ELECTION_DAY for e in COMPARE):
            c["usual"] = "election_day"
        elif not days or v["dbe"] is None:
            c["usual"] = "none"
        else:
            c["usual"] = "earlier" if v["dbe"] > min(days) else "same" if v["dbe"] == min(days) else "later"
    c["part"] = sum(1 for e in LAST5 if e in p or e in h)
    return c


def new_scope():
    return {
        "n": 0,
        "onestop": 0,
        "mail": 0,
        "daily": collections.Counter(),
        "first": collections.Counter(),
        "first_age": collections.defaultdict(lambda: [0, 0]),
        "part": collections.Counter(),
        "usual": collections.Counter(),
        **{f"t{e[:4]}": collections.Counter() for e in COMPARE},
        **{f"w{e[:4]}": collections.Counter() for e in COMPARE},
    }


def prior_daily():
    """{year: {county: {days_before: one-stop ballots}}} for every federal general election
    before 2026 (2002 on), over the widest early voting period (20 to 3 days out), cached."""
    src = CACHE / "ballots_federal.tsv.gz"
    cache = CACHE / "prior_daily_all.json"
    if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
        return json.load(open(cache))
    out = {e[:4]: collections.defaultdict(collections.Counter) for e in ELECTIONS if e != E26}
    with gzip.open(src, "rt") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            y = r["election"][:4]
            if y in out and r["method"] == "ONESTOP" and r["days_before"]:
                d = int(r["days_before"])
                if EV_LAST <= d <= 20:
                    out[y][r["county"]][d] += 1
                    out[y]["NC"][d] += 1
    cache.write_text(json.dumps(out))
    return json.loads(cache.read_text())


def analyze():
    sid, info = load_crosswalk()
    open26 = {s for (e, _, _), s in sid.items() if e == E26}
    v26, latest = load_2026(sid)
    print(f"2026 counted ballots: {len(v26):,}", file=sys.stderr)
    ncids = set(v26)
    prior = load_prior(ncids)
    print("prior ballots loaded", file=sys.stderr)
    hist = load_hist(ncids)
    print("voter history loaded", file=sys.stderr)

    scopes = collections.defaultdict(new_scope)
    # Site flows: where each earlier one-stop site's voters cast their 2026 ballot.
    flows = {e[:4]: collections.defaultdict(collections.Counter) for e in COMPARE}
    # ...and whether those voters are voting earlier, the same day or later than they did there.
    flow_timing = {e[:4]: collections.defaultdict(collections.Counter) for e in COMPARE}
    # 2026 sites: who is voting there.
    sites26 = collections.defaultdict(collections.Counter)
    unmatched26 = collections.Counter()
    for k, v in v26.items():
        c = classify(v, prior[k], hist[k], sid, open26)
        for s in (scopes["NC"], scopes[v["county"]]):
            s["n"] += 1
            s[v["method"].lower()] += 1
            if v["method"] == "ONESTOP":
                if v["dbe"] is not None and EV_LAST <= v["dbe"] <= EV_FIRST:
                    s["daily"][v["dbe"]] += 1
                s["first"][c["first"] or "returning"] += 1
                s["usual"][c["usual"]] += 1
                # First-time one-stop voters who have voted in NC before, by age.
                s["first_age"][v["age"]][1] += 1
                s["first_age"][v["age"]][0] += c["first"] in ("mail", "election_day")
            s["part"][c["part"]] += 1
            for e in COMPARE:
                y = e[:4]
                s[f"t{y}"][c[f"t{y}"]] += 1
                if f"w{y}" in c:
                    s[f"w{y}"][c[f"w{y}"]] += 1
        for e in COMPARE:
            y = e[:4]
            if c.get(f"from{y}"):
                dest = v["site"] if v["method"] == "ONESTOP" else "MAIL"
                flows[y][c[f"from{y}"]][dest or "UNMATCHED"] += 1
                flow_timing[y][c[f"from{y}"]][c[f"t{y}"]] += 1
        if v["method"] == "ONESTOP":
            if v["site"]:
                t = sites26[v["site"]]
                t["n"] += 1
                t["first"] += c["first"] is not None
                for e in COMPARE:
                    t[f"{e[:4]}_{c.get(f'w{e[:4]}', 'none')}"] += 1
            else:
                unmatched26[f"{v['county']}|{v['site_name']}"] += 1

    def finish(s):
        return {
            "n": s["n"],
            "onestop": s["onestop"],
            "mail": s["mail"],
            "daily": {str(d): n for d, n in sorted(s["daily"].items())},
            "first": dict(s["first"]),
            "first_age": {a: s["first_age"][a] for a in AGES if a in s["first_age"]},
            "part": {str(i): s["part"][i] for i in range(6)},
            "usual": dict(s["usual"]),
            **{f"t{e[:4]}": dict(s[f"t{e[:4]}"]) for e in COMPARE},
            **{f"w{e[:4]}": dict(s[f"w{e[:4]}"]) for e in COMPARE},
        }

    return {
        "through": latest.isoformat() if latest else None,
        "scopes": {k: finish(s) for k, s in sorted(scopes.items())},
        "flows": {y: {src: dict(d) for src, d in f.items()} for y, f in flows.items()},
        "flow_timing": {y: {src: dict(d) for src, d in f.items()} for y, f in flow_timing.items()},
        "sites26": {k: dict(t) for k, t in sites26.items()},
        "unmatched26": dict(unmatched26),
        "site_info": info,
        "prior_daily": prior_daily(),
    }


if __name__ == "__main__":
    res = analyze()
    (CACHE / "analysis_2026_voters.json").write_text(json.dumps(res))
    nc = res["scopes"]["NC"]
    print(json.dumps({k: nc[k] for k in ("n", "onestop", "mail", "first", "t2024", "w2024")}, indent=1))
