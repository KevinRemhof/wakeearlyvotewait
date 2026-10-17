#!/usr/bin/env python3
"""Cross-walk one-stop (early voting) sites across elections.

Site names come from the ballot records (``site_name`` in the NCSBE absentee
files). The NCSBE one-stop site-list PDFs are used only to attach an address to
each recorded name, so that renamed sites (e.g. "LL - LAKE LYNN COMMUNITY
CENTER" -> "LAKE LYNN COMMUNITY CENTER") are linked by address rather than
treated as one site removed and another added.

Inputs (ncsbe_cache/):  absentee_<ymd>.zip, onestop_sites_<ymd>.pdf
Output (ncsbe_cache/):  onestop_site_crosswalk.json  (one entry per physical site)
                        onestop_site_changes.json    (added/removed/renamed sites
                                                      by election change, county, name)
"""
import collections
import csv
import difflib
import io
import json
import re
import subprocess
import zipfile
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "ncsbe_cache"
ELECTIONS = ["20221108", "20241105", "20261103"]
CITY_RE = re.compile(r"^(?P<city>[A-Z .'-]+), NC (?P<zip>\d{5})")
TIME_RE = re.compile(r"\d{1,2}:\d{2}\s*[ap]\.?m\.?(\s*-\s*\d{1,2}:\d{2}\s*[ap]\.?m\.?)?", re.I)
DAYSPAN_RE = re.compile(r"(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s+[A-Z][a-z]+\s+\d+"
                        r"(\s*-\s*(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s+[A-Z][a-z]+\s+\d+)?")
DAY_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*day,")
SKIP_RE = re.compile(r"(Sites for the|Questions related|CLICK HERE|^County$|"
                     r"Board of Elections Office$|Sites in Addition)")
INSTEAD = "*Instead of voting at the County Elections Office*"


def norm(s):
    s = re.sub(r"^[A-Z]{2} - ", "", s.upper().strip())  # Wake-style 2-letter codes
    s = s.replace("&", " AND ").replace("COMMUN ITY", "COMMUNITY")
    return re.sub(r"[^A-Z0-9 ]", "", re.sub(r"\s+", " ", s)).strip()


def addr_key(county, street, zipc):
    m = re.match(r"(\d+)\s+(\S+)", street.upper())
    return f"{county}|{m.group(1)} {m.group(2) if m else ''}|{zipc}" if m else \
        f"{county}|{norm(street)}|{zipc}"


def parse_pdf(path, counties):
    """Return {county: [ {name, street, city, zip, replaces_office} ]}."""
    text = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                          capture_output=True, text=True, check=True).stdout
    sites = collections.defaultdict(list)
    county, bufs = None, collections.defaultdict(list)
    for line in text.splitlines():
        for m in re.finditer(r"\S(?:.*?\S)?(?=\s{3,}|$)", line):
            chunk = DAYSPAN_RE.sub("", TIME_RE.sub("", m.group(0))).strip()
            col = m.start()
            if not chunk:
                continue
            if DAY_RE.match(chunk) or SKIP_RE.search(chunk):
                continue
            if col < 8 and chunk in counties:
                county, bufs = chunk, collections.defaultdict(list)
                continue
            bucket = "office" if col < 40 else "extra"
            if chunk == INSTEAD:
                bufs[bucket].append(chunk)
                continue
            c = CITY_RE.match(chunk)
            if not c:
                bufs[bucket].append(chunk)
                continue
            lines = bufs.pop(bucket, [])
            instead = INSTEAD in lines
            lines = [l for l in lines if l != INSTEAD]
            if not lines:
                continue
            # Street = trailing lines that start with a digit/PO/suite; name = the rest.
            split = len(lines) - 1
            while split > 1 and re.match(r"^(\d|PO |P\.O|SUITE|STE|#)", lines[split - 1]):
                split -= 1
            sites[county].append({
                "name": " ".join(lines[:split]),
                "street": " ".join(lines[split:]),
                "city": c.group("city").strip(),
                "zip": c.group("zip"),
                "replaces_office": instead,
            })
    return sites


def record_sites(path):
    """Return {county: {site_name: one-stop ballots}} from the ballot records.

    Cached next to the zip, keyed by its modification time, so finished
    elections are only read once.
    """
    cache = path.with_name(path.stem + "_sites.json")
    stamp = path.stat().st_mtime
    if cache.exists():
        saved = json.loads(cache.read_text())
        if saved["mtime"] == stamp:
            return saved["sites"]
    z = zipfile.ZipFile(path)
    c = collections.defaultdict(collections.Counter)
    for r in csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="latin-1")):
        if r["ballot_req_type"] in ("ONE-STOP", "EARLY VOTING") and r["site_name"].strip():
            c[r["county_desc"].strip()][r["site_name"].strip()] += 1
    cache.write_text(json.dumps({"mtime": stamp, "sites": c}))
    return c


def match(name, pdf_sites):
    """Best PDF entry for a recorded site name, with a match score."""
    n = norm(name)
    best, score = None, 0.0
    for s in pdf_sites:
        p = norm(s["name"])
        r = 1.0 if n == p else difflib.SequenceMatcher(None, n, p).ratio()
        if n and (n in p or p in n):
            r = max(r, 0.9)
        if r > score:
            best, score = s, r
    return best, score


def changes(sites):
    """Added/removed/renamed physical sites by election change -> county -> name."""
    def entry(s, e):
        return {"name": s["elections"][e]["site_name"], "address": s["address"], "id": s["id"]}

    pairs = list(zip(ELECTIONS, ELECTIONS[1:])) + [(ELECTIONS[0], ELECTIONS[-1])]
    result = {}
    for old, new in pairs:
        by_county = collections.defaultdict(lambda: {"added": [], "removed": [], "renamed": []})
        for s in sites:
            o, n = old in s["elections"], new in s["elections"]
            c = by_county[s["county"]]
            if n and not o:
                c["added"].append(entry(s, new))
            elif o and not n:
                c["removed"].append(entry(s, old))
            elif o and n and norm(s["elections"][old]["site_name"]) != norm(s["elections"][new]["site_name"]):
                c["renamed"].append({"old_name": s["elections"][old]["site_name"],
                                     "name": s["elections"][new]["site_name"],
                                     "address": s["address"], "id": s["id"]})
        for c in by_county.values():
            for k in c:
                c[k].sort(key=lambda x: x["name"])
        result[f"{old}_to_{new}"] = {k: v for k, v in sorted(by_county.items())
                                     if v["added"] or v["removed"] or v["renamed"]}
    return {"note": "Physical sites matched by address (see onestop_site_crosswalk.json). "
                    "'renamed' = same address, different recorded name. A county office that "
                    "moved shows as one removed and one added site.",
            "changes": result}


def main():
    sites = {}  # addr_key or unmatched key -> site record
    unmatched = []
    for e in ELECTIONS:
        recs = record_sites(CACHE / f"absentee_{e}.zip")
        pdf = parse_pdf(CACHE / f"onestop_sites_{e}.pdf", set(recs))
        for county, names in recs.items():
            for name, votes in names.items():
                p, score = match(name, pdf.get(county, []))
                if p and score >= 0.6:
                    key = addr_key(county, p["street"], p["zip"])
                    addr = {k: p[k] for k in ("street", "city", "zip")}
                else:
                    key = f"{county}|UNMATCHED|{norm(name)}"
                    addr = None
                    unmatched.append({"county": county, "election": e,
                                      "site_name": name, "votes": votes})
                s = sites.setdefault(key, {"county": county, "address": addr,
                                           "elections": {}})
                s["elections"][e] = {
                    "site_name": name, "one_stop_ballots": votes,
                    "official_list_name": p["name"] if addr else None,
                    "official_list_address": addr,
                    "match_score": round(score, 2),
                    "replaces_county_office": bool(addr and p["replaces_office"]),
                }
    out = []
    for key, s in sorted(sites.items()):
        present = [e for e in ELECTIONS if e in s["elections"]]
        names = {s["elections"][e]["site_name"] for e in present}
        s["id"] = key
        s["present_in"] = present
        s["renamed"] = len({norm(n) for n in names}) > 1
        s["status_2026_vs_2022"] = (
            "kept" if {"20221108", "20261103"} <= set(present) else
            "added" if "20261103" in present and "20221108" not in present else
            "removed" if "20221108" in present and "20261103" not in present else
            "not_in_either")
        out.append(s)
    doc = {
        "description": __doc__.strip().splitlines()[0],
        "elections": ELECTIONS,
        "source": {"site_names_and_counts": "absentee_<ymd>.zip site_name (ONE-STOP / EARLY VOTING rows)",
                   "addresses": "onestop_sites_<ymd>.pdf (NCSBE official site lists)"},
        "note": "Sites are keyed by county + street number + first street word + ZIP. "
                "20261103 counts are partial until early voting ends.",
        "sites": out,
        "unmatched_record_names": unmatched,
    }
    (CACHE / "onestop_site_crosswalk.json").write_text(json.dumps(doc, indent=1))
    (CACHE / "onestop_site_changes.json").write_text(json.dumps(changes(out), indent=1))
    by = collections.Counter(s["status_2026_vs_2022"] for s in out)
    print(f"{len(out)} sites; {dict(by)}; {len(unmatched)} unmatched record names")


if __name__ == "__main__":
    main()
