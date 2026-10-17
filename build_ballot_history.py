#!/usr/bin/env python3
"""Normalize NCSBE absentee (one-stop + mail) ballot records for federal elections.

Reads ncsbe_cache/absentee_<ymd>.zip for every November general election and
writes one row per counted ballot to ncsbe_cache/ballots_federal.tsv.gz:

    election  ncid  county  voter_reg_num  method  rtn_dt  days_before  site_name

method       ONESTOP or MAIL (mail includes e-mail/fax military & overseas)
counted      only accepted ballots are written (OK / ACCEPTED*; for 2002-2004,
             which have no status column, any ballot with a return date)
ncid         present in the records from 2016 on. Earlier ballots are linked by
             county + voter_reg_num (unique within a county only) through a
             bridge built from ncvoter_Statewide.zip, ncvhis_Statewide.zip and
             the 2016+ ballot records; unlinked rows keep ncid empty.
"""
import csv
import datetime as dt
import gzip
import io
import sys
import zipfile
from pathlib import Path

csv.field_size_limit(10**7)
CACHE = Path(__file__).resolve().parent / "ncsbe_cache"
ELECTIONS = ["20021105", "20041102", "20061107", "20081104", "20101102", "20121106",
             "20141104", "20161108", "20181106", "20201103", "20221108", "20241105",
             "20261103"]
ONESTOP = {"OS", "ONE STOP", "ONE-STOP", "EARLY VOTING"}
OUT_COLS = ["election", "ncid", "county", "voter_reg_num", "method", "rtn_dt",
            "days_before", "site_name"]


def zip_rows(path, sep=None):
    """Yield dicts (lower-cased, underscore headers) from the single file in a zip."""
    z = zipfile.ZipFile(path)
    f = io.TextIOWrapper(z.open(z.namelist()[0]), encoding="latin-1", newline="")
    head = f.readline()
    sep = sep or ("\t" if "\t" in head else ",")
    hdr = [h.strip().strip('"').lower().replace(" ", "_")
           for h in next(csv.reader([head], delimiter=sep))]
    for r in csv.reader(f, delimiter=sep):
        yield dict(zip(hdr, (v.strip() for v in r)))


def parse_date(s):
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def counted(r, e):
    status = r.get("ballot_rtn_status")
    if status is None:  # 2002, 2004
        return bool(r.get("ballot_rtn_dt"))
    return status == "OK" or status.startswith("ACCEPTED")


def build_bridge():
    """(county, zero-padded reg num) -> ncid from every source that carries both."""
    bridge = {}

    def add(county, reg, ncid):
        if county and reg and ncid:
            bridge.setdefault((county.upper(), reg.zfill(12)), ncid)

    for r in zip_rows(CACHE / "ncvoter_Statewide.zip", "\t"):
        add(r["county_desc"], r["voter_reg_num"], r["ncid"])
    print(f"bridge after ncvoter: {len(bridge):,}", file=sys.stderr)
    for r in zip_rows(CACHE / "ncvhis_Statewide.zip", "\t"):
        add(r["county_desc"], r["voter_reg_num"], r["ncid"])
    print(f"bridge after ncvhis: {len(bridge):,}", file=sys.stderr)
    for e in ELECTIONS:
        if e >= "20161108":
            for r in zip_rows(CACHE / f"absentee_{e}.zip"):
                add(r["county_desc"], r["voter_reg_num"], r.get("ncid", ""))
    print(f"bridge after ballots: {len(bridge):,}", file=sys.stderr)
    return bridge


def main():
    bridge = build_bridge()
    out = gzip.open(CACHE / "ballots_federal.tsv.gz", "wt", newline="")
    w = csv.writer(out, delimiter="\t", lineterminator="\n")
    w.writerow(OUT_COLS)
    for e in ELECTIONS:
        eday = dt.datetime.strptime(e, "%Y%m%d").date()
        best = {}  # one counted ballot per voter per election (latest return wins)
        n = linked = 0
        for r in zip_rows(CACHE / f"absentee_{e}.zip"):
            if not counted(r, e):
                continue
            county = r["county_desc"].upper()
            reg = r["voter_reg_num"].zfill(12)
            ncid = r.get("ncid", "") or bridge.get((county, reg), "")
            req = (r.get("ballot_req_type") or r.get("ballot_request_type") or "").upper()
            d = parse_date(r.get("ballot_rtn_dt", ""))
            row = [e, ncid, county, reg, "ONESTOP" if req in ONESTOP else "MAIL",
                   d.isoformat() if d else "", (eday - d).days if d else "",
                   r.get("site_name", "")]
            key = ncid or (county, reg)
            if key not in best or row[5] >= best[key][5]:
                best[key] = row
            n += 1
        for row in best.values():
            linked += bool(row[1])
            w.writerow(row)
        print(f"{e}: {n:,} counted rows, {len(best):,} voters, "
              f"{linked / max(len(best), 1):.1%} linked to ncid", file=sys.stderr)
    out.close()


if __name__ == "__main__":
    main()
