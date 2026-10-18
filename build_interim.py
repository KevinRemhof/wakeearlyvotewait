#!/usr/bin/env python3
"""Build interim data sets for statewide analysis, as Parquet in ncsbe_cache/interim/.

  ./build_interim.py                  # everything
  ./build_interim.py votes places     # some steps: sources votes places results registration

Data sets (one row per ...):
  voters.parquet           voter: current party, race, ethnicity, sex, birth year
  votes.parquet            voter and election: how and where they voted, party at the time
  voter_elections.parquet  voter, wide: one column per election holding where they voted
                           (a place_id, MAIL, or the method when the place isn't known),
                           empty when there's no record of a vote
  places.parquet           voting place: address, county, which elections it was an Election
                           Day polling place or a one-stop site, and whether it was one-stop only
  place_elections.parquet  place and election: how it was used (and precincts or site name)
  county_results.parquet   county, general election, contest and party: votes and share
  registration_stats.parquet    registered voters at each general by county, precinct, party,
                                race, ethnicity, sex and age group (State Board voter_stats)
  registration_current.parquet  voter: current registration status, county, precinct, districts

Coverage: every November general since 2002 from the absentee files (mail and one-stop
ballots only before Nov 2016, so Election Day voters are missing from those years), and
every election since Nov 2016 from the voter history file (all methods). Voters are keyed
by ncid; ballots from before 2016 carry only county + registration number, linked to an
ncid where any source carries both, and keyed "COUNTY:regnum" otherwise.

No names, street addresses or phone numbers are copied into these files.
Large inputs are streamed from their zips through a pipe rather than extracted.
"""

import collections
import csv
import datetime as dt
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

import duckdb

import build_ballot_history as bbh
from build_site_crosswalk import addr_key, norm

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "ncsbe_cache"
OUT = CACHE / "interim"
SRC = OUT / "_src"
BASE = "https://s3.amazonaws.com/dl.ncsbe.gov/ENRS"
GENERALS = bbh.ELECTIONS  # 2002 on: the first general with ballot-level records
HIST_START = "20161108"  # the voter history file starts here
ONESTOP_REQ = {"OS", "ONE STOP", "ONE-STOP", "EARLY VOTING"}
# Voter history voting_method -> method.
METHODS = {
    "ABSENTEE ONESTOP": "ONESTOP",
    "EARLY VOTING IN-PERSON": "ONESTOP",
    "ABSENTEE CURBSIDE": "ONESTOP",
    "EARLY VOTING CURBSIDE": "ONESTOP",
    "ELECTION DAY IN-PERSON": "ELECTION_DAY",
    "ELECTION DAY CURBSIDE": "ELECTION_DAY",
    "PROVISIONAL": "ELECTION_DAY",
    "TRANSFER": "ELECTION_DAY",
    "ABSENTEE BY MAIL": "MAIL",
    "ABSENTEE": "MAIL",
}
# Absentee-file race descriptions -> voter file race codes.
RACES = [
    ("WHITE", "W"),
    ("BLACK", "B"),
    ("ASIAN", "A"),
    ("INDIAN", "I"),
    ("TWO", "M"),
    ("MULTI", "M"),
    ("PACIFIC", "P"),
    ("HAWAIIAN", "P"),
    ("OTHER", "O"),
    ("UNDESIG", "U"),
]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def connect():
    con = duckdb.connect()
    tmp = OUT / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.sql(
        f"SET temp_directory='{tmp}'; SET preserve_insertion_order=false; SET enable_progress_bar=false; "
        "SET memory_limit='10GB'; SET max_temp_directory_size='6GiB'"
    )
    return con


def fetch(folder_date, name, dest=None):
    """Download ENRS/<folder>/<name> into the cache unless it's there. None if missing."""
    dest = dest or CACHE / name
    if dest.exists():
        return dest
    d = f"{folder_date[:4]}_{folder_date[4:6]}_{folder_date[6:]}"
    try:
        log(f"downloading {name}")
        urllib.request.urlretrieve(f"{BASE}/{d}/{name}", dest)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise
    return dest


def header(zpath, encoding="latin-1"):
    """(member, delimiter, [lower-cased column names]) of the delimited file in a zip."""
    z = zipfile.ZipFile(zpath)
    member = max(z.namelist(), key=lambda n: z.getinfo(n).file_size)
    with z.open(member) as f:
        line = io.TextIOWrapper(f, encoding=encoding, newline="").readline()
    delim = "\t" if "\t" in line else ","
    cols = [
        c.strip().strip('"').strip().lower().replace(" ", "_")
        for c in next(csv.reader([line], delimiter=delim))
    ]
    return member, delim, [c or f"col{i}" for i, c in enumerate(cols)]


def stream(con, zpath, select, out, encoding="latin-1", columns=None):
    """COPY (select over table `t`, the zip's delimited file read as VARCHAR) TO out.parquet.
    The file goes through a named pipe, so it's never written to disk uncompressed."""
    member, delim, cols = header(zpath, encoding)
    has_header = columns is None
    cols = columns or cols
    if not member.lower().endswith((".txt", ".csv")):
        raise ValueError(f"{zpath.name} holds {member}, not a delimited text file")
    with tempfile.TemporaryDirectory(dir=OUT / "_tmp") as d:
        fifo = os.path.join(d, "pipe")
        os.mkfifo(fifo)

        def feed():  # unzip | iconv -> pipe, as UTF-8
            with open(fifo, "wb") as w:
                unz = subprocess.Popen(
                    ["unzip", "-p", str(zpath), member], stdout=subprocess.PIPE
                )
                subprocess.run(
                    [
                        "iconv",
                        "-f",
                        {"latin-1": "ISO-8859-1"}.get(encoding, encoding),
                        "-t",
                        "UTF-8",
                    ],
                    stdin=unz.stdout,
                    stdout=w,
                    check=False,
                )
                unz.stdout.close()
                unz.wait()

        th = threading.Thread(target=feed)
        th.start()
        spec = ", ".join(f"'{c}': 'VARCHAR'" for c in cols)
        try:
            con.sql(f"""COPY (WITH t AS (SELECT * FROM read_csv('{fifo}', delim='{delim}', header={str(has_header).lower()}, quote='"',
                         escape='"', columns={{{spec}}}, auto_detect=false, null_padding=true, strict_mode=false,
                         parallel=false))
                         {select(set(cols))}) TO '{out}' (FORMAT parquet, COMPRESSION zstd)""")
        finally:
            th.join()


def pick(cols, *names, default="NULL::VARCHAR"):
    """SQL for the first of these columns present, trimmed and empty -> NULL."""
    for n in names:
        if n in cols:
            return f"nullif(trim({n}), '')"
    return default


# ---------------------------------------------------------------- sources


def hist_elections(con):
    return [
        r[0]
        for r in con.sql(
            f"SELECT DISTINCT election FROM '{SRC}/ncvhis.parquet' ORDER BY 1"
        ).fetchall()
    ]


def src_ncvoter(con):
    z, out = CACHE / "ncvoter_Statewide.zip", SRC / "ncvoter.parquet"
    if out.exists() and out.stat().st_mtime > z.stat().st_mtime:
        return
    log("ncvoter -> parquet")
    keep = [
        "county_desc",
        "voter_reg_num",
        "ncid",
        "status_cd",
        "voter_status_desc",
        "reason_cd",
        "voter_status_reason_desc",
        "registr_dt",
        "race_code",
        "ethnic_code",
        "party_cd",
        "gender_code",
        "birth_year",
        "res_city_desc",
        "zip_code",
        "precinct_abbrv",
        "precinct_desc",
        "municipality_desc",
        "ward_desc",
        "cong_dist_abbrv",
        "nc_senate_abbrv",
        "nc_house_abbrv",
        "super_court_abbrv",
        "judic_dist_abbrv",
        "county_commiss_desc",
        "school_dist_desc",
        "vtd_abbrv",
    ]
    stream(
        con,
        z,
        lambda cols: (
            "SELECT "
            + ", ".join(
                "lpad(trim(voter_reg_num), 12, '0') AS voter_reg_num"
                if c == "voter_reg_num"
                else "try_strptime(nullif(trim(registr_dt), ''), '%m/%d/%Y')::DATE AS registr_dt"
                if c == "registr_dt"
                else "try_cast(nullif(trim(birth_year), '') AS SMALLINT) AS birth_year"
                if c == "birth_year"
                else f"{pick(cols, c)} AS {c}"
                for c in keep
            )
            + " FROM t"
        ),
        out,
    )


def src_ncvhis(con):
    z, out = CACHE / "ncvhis_Statewide.zip", SRC / "ncvhis.parquet"
    if out.exists() and out.stat().st_mtime > z.stat().st_mtime:
        return
    log("ncvhis -> parquet")
    stream(
        con,
        z,
        lambda cols: (
            """SELECT strftime(strptime(trim(election_lbl), '%m/%d/%Y'), '%Y%m%d') AS election,
        trim(regexp_replace(election_desc, '^\\S+\\s+', '')) AS election_desc,
        nullif(trim(ncid), '') AS ncid, upper(trim(county_desc)) AS county,
        lpad(trim(voter_reg_num), 12, '0') AS voter_reg_num, trim(voting_method) AS voting_method,
        nullif(trim(voted_party_cd), '') AS party, upper(trim(voted_county_desc)) AS voted_county,
        nullif(trim(pct_label), '') AS precinct, nullif(trim(pct_description), '') AS precinct_desc,
        nullif(trim(vtd_label), '') AS vtd
        FROM t WHERE trim(voting_method) <> 'ELIGIBLE DID NOT VOTE'"""
        ),
        out,
    )


def src_absentee(con, e):
    out = SRC / f"absentee_{e}.parquet"
    z = CACHE / f"absentee_{e}.zip"
    if out.exists() and (not z.exists() or out.stat().st_mtime > z.stat().st_mtime):
        return
    keep_zip = e in GENERALS  # other scripts use the generals; others are deleted after
    if not fetch(e, z.name):
        log(f"no absentee file for {e}")
        return
    log(f"absentee {e} -> parquet")
    req = lambda cols: pick(cols, "ballot_req_type", "ballot_request_type")
    status = lambda cols: pick(cols, "ballot_rtn_status")
    date = lambda c: (
        f"coalesce(try_strptime({c}, '%m/%d/%Y'), try_strptime({c}, '%Y%m%d'))::DATE"
    )

    def race(cols):
        r = pick(cols, "race")
        return (
            "CASE WHEN length("
            + r
            + ") = 1 THEN upper("
            + r
            + ") "
            + " ".join(
                f"WHEN upper({r}) LIKE '%{w}%' THEN '{code}'" for w, code in RACES
            )
            + " END"
        )

    stream(
        con,
        z,
        lambda cols: (
            f"""SELECT '{e}' AS election, {pick(cols, "ncid")} AS ncid,
        upper(trim(county_desc)) AS county, lpad(trim(voter_reg_num), 12, '0') AS voter_reg_num,
        {race(cols)} AS race_code,
        {pick(cols, "ethnicity")} AS ethnicity, {pick(cols, "gender")} AS gender,
        try_cast({pick(cols, "age")} AS SMALLINT) AS age,
        {pick(cols, "voter_party_code", "voter_party")} AS party,
        CASE WHEN upper({req(cols)}) IN ({", ".join(f"'{m}'" for m in ONESTOP_REQ)}) THEN 'ONESTOP' ELSE 'MAIL' END AS method,
        {date(pick(cols, "ballot_rtn_dt"))} AS rtn_dt, {status(cols)} AS status,
        CASE WHEN {status(cols)} IS NULL THEN {pick(cols, "ballot_rtn_dt")} IS NOT NULL
             ELSE {status(cols)} = 'OK' OR {status(cols)} LIKE 'ACCEPTED%' END AS counted,
        upper({pick(cols, "site_name")}) AS site_name, {pick(cols, "precinct_desc")} AS precinct_desc
        FROM t"""
        ),
        out,
    )
    if not keep_zip:
        z.unlink()


def src_polling_places(con, e):
    out = SRC / f"polling_place_{e}.parquet"
    if out.exists() and e != max(GENERALS):
        return
    path = fetch(e, f"polling_place_{e}.csv", SRC / f"polling_place_{e}.csv")
    if not path:
        return
    raw = path.read_bytes()
    text = (
        raw.decode("utf-16")
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff")
        else raw.decode("latin-1")
    )
    rows = list(csv.DictReader(io.StringIO(text), delimiter="\t"))
    path.unlink()
    recs = []
    for r in rows:
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        street = r.get("street_address") or " ".join(
            x for x in (r.get("house_num"), r.get("street_name")) if x
        )
        county = r["county_name"].upper()
        recs.append(
            (
                e,
                county,
                r.get("polling_place_id"),
                r.get("polling_place_name", "").upper(),
                r.get("precinct_name", "").upper(),
                street.upper(),
                r.get("city", "").upper(),
                r.get("zip", "")[:5],
                addr_key(county, street, r.get("zip", "")[:5]) if street else None,
            )
        )
    con.sql(
        "CREATE OR REPLACE TEMP TABLE pp (election VARCHAR, county VARCHAR, polling_place_id VARCHAR, name VARCHAR, "
        "precinct VARCHAR, street VARCHAR, city VARCHAR, zip VARCHAR, place_id VARCHAR)"
    )
    con.executemany("INSERT INTO pp VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", recs)
    con.sql(f"COPY pp TO '{out}' (FORMAT parquet)")


def build_sources(con):
    SRC.mkdir(parents=True, exist_ok=True)
    src_ncvoter(con)
    src_ncvhis(con)
    elections = sorted(set(GENERALS) | set(hist_elections(con)))
    for e in elections:
        src_absentee(con, e)
        if e >= "20080101":
            src_polling_places(con, e)
    tmp = OUT / "_tmp"
    for f in tmp.glob("*"):
        if f.is_file():
            f.unlink()


# ---------------------------------------------------------------- 1) voters and votes


def build_votes(con):
    log("votes")
    hist = hist_elections(con)
    absentee = f"'{SRC}/absentee_*.parquet'"
    # County + registration number -> ncid, from every source that carries both.
    con.sql(f"""CREATE OR REPLACE TEMP TABLE bridge AS
        SELECT county, voter_reg_num, any_value(ncid) AS ncid FROM (
            SELECT upper(county_desc) AS county, voter_reg_num, ncid FROM '{SRC}/ncvoter.parquet'
            UNION ALL SELECT county, voter_reg_num, ncid FROM '{SRC}/ncvhis.parquet'
            UNION ALL SELECT county, voter_reg_num, ncid FROM read_parquet({absentee}))
        WHERE ncid IS NOT NULL GROUP BY ALL""")
    # One counted absentee/one-stop ballot per voter per election, the latest returned.
    con.sql(f"""CREATE OR REPLACE TEMP TABLE ab AS
        SELECT a.* EXCLUDE (ncid), coalesce(a.ncid, b.ncid, a.county || ':' || a.voter_reg_num) AS voter_id,
               coalesce(a.ncid, b.ncid) IS NOT NULL AS linked
        FROM read_parquet({absentee}) a LEFT JOIN bridge b USING (county, voter_reg_num)
        WHERE counted
        QUALIFY row_number() OVER (PARTITION BY election, voter_id ORDER BY rtn_dt DESC NULLS LAST) = 1""")
    resolve_sites(con)
    method = (
        "CASE voting_method "
        + " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in METHODS.items())
        + " ELSE 'OTHER' END"
    )
    # Election Day polling place by election, county and precinct name.
    con.sql(f"""CREATE OR REPLACE TEMP TABLE pct_place AS
        SELECT election, county, precinct, any_value(place_id) AS place_id
        FROM '{SRC}/polling_place_*.parquet' WHERE place_id IS NOT NULL GROUP BY ALL""")
    hist_sql = ", ".join(f"'{e}'" for e in hist)
    con.sql(f"""CREATE OR REPLACE TEMP TABLE votes AS
        WITH h AS (
            SELECT election, election_desc, ncid AS voter_id, voted_county AS county, {method} AS method,
                   voting_method, party, precinct, precinct_desc
            FROM '{SRC}/ncvhis.parquet'
            QUALIFY row_number() OVER (PARTITION BY election, ncid ORDER BY voting_method) = 1)
        SELECT h.election, h.election_desc, h.voter_id, h.county, h.method, h.voting_method, h.party,
               h.precinct, ab.rtn_dt AS ballot_date,
               CASE WHEN h.method = 'ONESTOP' THEN s.place_id WHEN h.method = 'ELECTION_DAY' THEN p.place_id END AS place_id,
               CASE WHEN h.method = 'ONESTOP' THEN ab.site_name END AS site_name,
               'history' AS source
        FROM h
        LEFT JOIN ab ON ab.election = h.election AND ab.voter_id = h.voter_id
        LEFT JOIN sites s ON s.election = h.election AND s.county = ab.county AND s.site_name = ab.site_name
        LEFT JOIN pct_place p ON p.election = h.election AND p.county = h.county AND p.precinct = h.precinct_desc
        UNION ALL
        SELECT ab.election, 'GENERAL', ab.voter_id, ab.county, ab.method,
               NULL, ab.party, NULL, ab.rtn_dt, s.place_id, ab.site_name, 'absentee'
        FROM ab LEFT JOIN sites s ON s.election = ab.election AND s.county = ab.county AND s.site_name = ab.site_name
        WHERE ab.election NOT IN ({hist_sql})""")
    con.sql(
        f"COPY (SELECT * FROM votes ORDER BY election, county) TO '{OUT}/votes.parquet' (FORMAT parquet, COMPRESSION zstd)"
    )

    # Voter attributes: the current registration where there is one (the active record first),
    # otherwise the latest ballot record, with birth year from the age then.
    con.sql(f"""COPY (
        WITH reg AS (
            SELECT ncid AS voter_id, upper(county_desc) AS county, status_cd, party_cd AS party, race_code,
                   ethnic_code, gender_code, birth_year, 'voter file' AS attrs_from
            FROM '{SRC}/ncvoter.parquet' WHERE ncid IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY ncid ORDER BY status_cd = 'A' DESC, status_cd = 'I' DESC,
                                       registr_dt DESC NULLS LAST) = 1),
        ballot AS (
            SELECT voter_id, county, NULL AS status_cd, party, race_code,
                   CASE WHEN ethnicity ILIKE 'HISPANIC%' THEN 'HL' WHEN ethnicity ILIKE 'NOT HISP%' THEN 'NL'
                        WHEN ethnicity IS NOT NULL THEN 'UN' END AS ethnic_code, gender AS gender_code,
                   (left(election, 4)::INT - age)::SMALLINT AS birth_year, 'ballot record' AS attrs_from
            FROM ab QUALIFY row_number() OVER (PARTITION BY voter_id ORDER BY election DESC) = 1),
        ids AS (SELECT DISTINCT voter_id FROM votes)
        SELECT ids.voter_id, ids.voter_id NOT LIKE '%:%' AS has_ncid, coalesce(r.county, b.county) AS county,
               r.status_cd, coalesce(r.party, b.party) AS party, coalesce(r.race_code, b.race_code) AS race_code,
               coalesce(r.ethnic_code, b.ethnic_code) AS ethnic_code, coalesce(r.gender_code, b.gender_code) AS gender_code,
               coalesce(r.birth_year, b.birth_year) AS birth_year, coalesce(r.attrs_from, b.attrs_from) AS attrs_from
        FROM ids LEFT JOIN reg r USING (voter_id) LEFT JOIN ballot b USING (voter_id)
        ) TO '{OUT}/voters.parquet' (FORMAT parquet, COMPRESSION zstd)""")

    report_votes(con)
    for t in ("votes", "ab", "bridge"):
        con.sql(f"DROP TABLE IF EXISTS {t}")
    build_wide(con)


def build_wide(con, parts=8):
    """voter_elections.parquet: one column per election, built in slices of voters to bound memory."""
    log("voter_elections (wide)")
    votes = f"'{OUT}/votes.parquet'"
    elections = [
        r[0]
        for r in con.sql(f"SELECT DISTINCT election FROM {votes} ORDER BY 1").fetchall()
    ]
    cols = ", ".join(
        f"""max(CASE WHEN election = '{e}' THEN coalesce(place_id, method) END) AS e{e}"""
        for e in elections
    )
    files = []
    for i in range(parts):
        f = OUT / "_tmp" / f"wide_{i}.parquet"
        con.sql(f"""COPY (
            SELECT v.voter_id, v.party, v.race_code, v.birth_year, w.* EXCLUDE (voter_id)
            FROM (SELECT voter_id, {cols} FROM {votes} WHERE hash(voter_id) % {parts} = {i} GROUP BY voter_id) w
            JOIN '{OUT}/voters.parquet' v USING (voter_id)
            ) TO '{f}' (FORMAT parquet, COMPRESSION zstd)""")
        files.append(f"'{f}'")
    con.sql(f"""COPY (SELECT * FROM read_parquet([{", ".join(files)}]))
        TO '{OUT}/voter_elections.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    for f in files:
        Path(f.strip("'")).unlink()


def resolve_sites(con):
    """sites: (election, county, one-stop site name) -> place_id. In order: the address crosswalk
    (2022 on), then the same name at a crosswalk site, then at an Election Day polling place,
    then a name-only id."""
    import json

    xw = json.loads((CACHE / "onestop_site_crosswalk.json").read_text())["sites"]
    exact, by_name, info = {}, {}, {}
    for s in xw:
        a = s["address"] or {}
        for e, v in s["elections"].items():
            exact[(e, s["county"], v["site_name"].upper())] = s["id"]
            by_name.setdefault((s["county"], norm(v["site_name"])), s["id"])
        if a:
            latest = s["elections"][max(s["elections"])]
            info[s["id"]] = (
                s["county"],
                latest["site_name"].upper(),
                a["street"],
                a["city"],
                a["zip"],
            )
    pp = {}
    for (
        county,
        name,
        place_id,
    ) in con.sql(f"""SELECT county, name, place_id FROM '{SRC}/polling_place_*.parquet'
                                              WHERE place_id IS NOT NULL ORDER BY election DESC""").fetchall():
        pp.setdefault((county, norm(name)), place_id)
    rows = []
    for e, county, name, n in con.sql(
        "SELECT election, county, site_name, count(*) FROM ab WHERE method = 'ONESTOP' "
        "AND site_name IS NOT NULL GROUP BY ALL"
    ).fetchall():
        k = (county, norm(name))
        if (e, county, name) in exact:
            pid, how = exact[(e, county, name)], "address"
        elif k in by_name:
            pid, how = by_name[k], "name, one-stop site"
        elif k in pp:
            pid, how = pp[k], "name, polling place"
        else:
            pid, how = f"{county}|NAME:{norm(name)}", "name only"
        rows.append((e, county, name, pid, how, n))
    con.sql(
        "CREATE OR REPLACE TEMP TABLE sites (election VARCHAR, county VARCHAR, site_name VARCHAR, "
        "place_id VARCHAR, matched_by VARCHAR, ballots INT)"
    )
    con.executemany("INSERT INTO sites VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.sql(
        "CREATE OR REPLACE TEMP TABLE site_info (place_id VARCHAR, county VARCHAR, name VARCHAR, street VARCHAR, "
        "city VARCHAR, zip VARCHAR)"
    )
    con.executemany(
        "INSERT INTO site_info VALUES (?, ?, ?, ?, ?, ?)",
        [(k, *v) for k, v in info.items()],
    )
    con.sql(f"COPY sites TO '{SRC}/onestop_site_names.parquet' (FORMAT parquet)")
    con.sql(f"COPY site_info TO '{SRC}/onestop_site_info.parquet' (FORMAT parquet)")
    for how, n, b in con.sql(
        "SELECT matched_by, count(*), sum(ballots) FROM sites GROUP BY 1 ORDER BY 3 DESC"
    ).fetchall():
        log(f"  one-stop site names matched by {how}: {n:,} names, {b:,} ballots")


def report_votes(con):
    log(
        con.sql("""SELECT election, any_value(election_desc) AS kind, count(*) AS votes,
        round(100 * avg((voter_id NOT LIKE '%:%')::INT), 1) AS pct_ncid,
        round(100 * avg((place_id IS NOT NULL)::INT) FILTER (WHERE method <> 'MAIL'), 1) AS pct_place_known,
        round(100 * avg((place_id NOT LIKE '%|NAME:%')::INT) FILTER (WHERE method <> 'MAIL'), 1) AS pct_address_known
        FROM votes GROUP BY election ORDER BY election""")
        .fetchall()
        .__repr__()
        .replace("), (", "),\n (")
    )


# ---------------------------------------------------------------- 2) places


def build_places(con):
    log("places")
    if not (SRC / "onestop_site_names.parquet").exists():
        build_votes(con)
    con.sql(
        f"CREATE OR REPLACE TEMP TABLE sites AS FROM '{SRC}/onestop_site_names.parquet'"
    )
    con.sql(
        f"CREATE OR REPLACE TEMP TABLE site_info AS FROM '{SRC}/onestop_site_info.parquet'"
    )
    con.sql(f"""CREATE OR REPLACE TEMP TABLE pe AS
        SELECT place_id, election, 'election_day' AS used_for, county, name, street, city, zip,
               string_agg(DISTINCT precinct, '; ' ORDER BY precinct) AS detail
        FROM '{SRC}/polling_place_*.parquet' WHERE place_id IS NOT NULL GROUP BY place_id, election, county, name, street, city, zip
        UNION ALL
        SELECT s.place_id, s.election, 'onestop', s.county, coalesce(i.name, s.site_name), i.street, i.city, i.zip,
               s.site_name
        FROM sites s LEFT JOIN site_info i USING (place_id)""")
    # One-stop sites matched by name to a polling place take that polling place's address.
    con.sql("""UPDATE pe SET street = q.street, city = q.city, zip = q.zip FROM (
        SELECT place_id, arg_max(street, election) AS street, arg_max(city, election) AS city, arg_max(zip, election) AS zip
        FROM pe WHERE used_for = 'election_day' GROUP BY 1) q
        WHERE pe.place_id = q.place_id AND pe.street IS NULL""")
    con.sql(f"""COPY (SELECT place_id, election, used_for, county, name, detail FROM pe ORDER BY place_id, election)
        TO '{OUT}/place_elections.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    con.sql(f"""COPY (
        SELECT place_id, any_value(county) AS county, arg_max(name, election) AS name,
               arg_max(street, election) FILTER (WHERE street IS NOT NULL) AS street,
               arg_max(city, election) FILTER (WHERE street IS NOT NULL) AS city,
               arg_max(zip, election) FILTER (WHERE street IS NOT NULL) AS zip,
               list(DISTINCT election ORDER BY election) FILTER (WHERE used_for = 'election_day') AS election_day_elections,
               list(DISTINCT election ORDER BY election) FILTER (WHERE used_for = 'onestop') AS onestop_elections,
               CASE WHEN place_id NOT LIKE '%|NAME:%'      -- no address: can't tell
                    THEN count(*) FILTER (WHERE used_for = 'election_day') = 0 END AS onestop_only,
               place_id LIKE '%|NAME:%' AS name_only
        FROM pe GROUP BY place_id ORDER BY county, name
        ) TO '{OUT}/places.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    log(
        con.sql(f"""SELECT count(*) places, count(*) FILTER (WHERE onestop_only) onestop_only,
        count(*) FILTER (WHERE len(onestop_elections) > 0 AND NOT onestop_only) both_uses,
        count(*) FILTER (WHERE name_only) name_only FROM '{OUT}/places.parquet'""").fetchall()
    )


# ---------------------------------------------------------------- 3) county results

OFFICES = [
    ("PRESIDENT", r"^(US |U\.S\. )?PRESIDENT"),
    ("US SENATE", r"^(US|U\.S\.|UNITED STATES) SENATE"),
    ("US HOUSE", r"^(US|U\.S\.|UNITED STATES) (HOUSE|CONGRESS)"),
    ("GOVERNOR", r"^(NC )?GOVERNOR"),
]


def results_rows(e):
    """Yield (county, contest, party, votes) for every precinct row of a general's results."""
    z = zipfile.ZipFile(fetch(e, f"results_pct_{e}.zip"))
    member = max(z.namelist(), key=lambda n: z.getinfo(n).file_size)
    f = io.TextIOWrapper(z.open(member), encoding="latin-1", newline="")
    first = f.readline()
    delim = "\t" if "\t" in first else ","
    if first.split(delim)[0].strip('" ').lower() != "county":
        # 2000 and 2002 have no header: county, date, precinct code, precinct, contest, choice, party, votes, stamp
        for r in csv.reader(itertools_chain([first], f), delimiter=delim):
            if len(r) >= 8:
                yield r[0], r[4], r[6], r[7]
        return
    hdr = [
        h.strip().strip('"').strip().lower().replace(" ", "_")
        for h in next(csv.reader([first], delimiter=delim))
    ]
    col = lambda *ns: next(hdr.index(n) for n in ns if n in hdr)
    ci, ki = col("county"), col("contest_name", "contest")
    pi, vi = (
        col("choice_party", "party_cd", "party"),
        col("total_votes", "ballot_count"),
    )
    for r in csv.reader(f, delimiter=delim):
        if len(r) > max(ci, ki, pi, vi):
            yield r[ci], r[ki], r[pi], r[vi]


def itertools_chain(*its):
    for it in its:
        yield from it


def build_results(con):
    log("county results")
    agg = collections.Counter()
    for e in GENERALS:
        if e == max(GENERALS) and not (CACHE / f"results_pct_{e}.zip").exists():
            continue  # the current election has no results yet
        for county, contest, party, votes in results_rows(e):
            try:
                v = int(float(votes))
            except ValueError:
                continue
            contest = re.sub(r"\s+", " ", contest.strip().upper())
            if contest != "STRAIGHT PARTY":
                agg[
                    (
                        e,
                        county.strip().upper(),
                        contest,
                        party.strip().upper() or "NONE",
                    )
                ] += v
    con.sql(
        "CREATE OR REPLACE TEMP TABLE r (election VARCHAR, county VARCHAR, contest VARCHAR, party VARCHAR, votes BIGINT)"
    )
    con.executemany(
        "INSERT INTO r VALUES (?, ?, ?, ?, ?)", [(*k, v) for k, v in agg.items()]
    )
    office = (
        "CASE "
        + " ".join(
            f"WHEN regexp_matches(contest, '{rx}') THEN '{o}'" for o, rx in OFFICES
        )
        + " END"
    )
    con.sql(f"""COPY (
        WITH partisan AS (   -- contests with at least one party-labeled candidate
            SELECT election, contest FROM r GROUP BY ALL
            HAVING count(*) FILTER (WHERE party NOT IN ('NONE', 'UNA', 'WRITE-IN')) > 0),
        c AS (SELECT r.*, {office} AS office FROM r JOIN partisan USING (election, contest)),
        house AS (   -- every US House district in a county, combined
            SELECT election, county, 'US HOUSE (ALL DISTRICTS)' AS contest, party, sum(votes) AS votes, 'US HOUSE' AS office
            FROM c WHERE office = 'US HOUSE' GROUP BY ALL),
        allc AS (SELECT * FROM c UNION ALL SELECT * FROM house)
        SELECT election, county, contest, office, party, votes::BIGINT AS votes,
               (sum(votes) OVER (PARTITION BY election, county, contest))::BIGINT AS contest_votes,
               round(100.0 * votes / nullif(sum(votes) OVER (PARTITION BY election, county, contest), 0), 2) AS pct,
               contest = CASE WHEN left(election, 4)::INT % 4 = 0 THEN 'PRESIDENT'
                              ELSE coalesce((SELECT max(contest) FROM c c2 WHERE c2.election = allc.election
                                             AND c2.office = 'US SENATE'), 'US HOUSE (ALL DISTRICTS)') END
                 OR (left(election, 4)::INT % 4 = 0 AND office = 'PRESIDENT') AS top_of_ticket
        FROM allc WHERE county NOT IN ('', 'STATEWIDE')
        ORDER BY election, county, contest, votes DESC
        ) TO '{OUT}/county_results.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    log(
        con.sql(f"""SELECT election, any_value(contest), count(DISTINCT county),
        round(sum(votes) FILTER (WHERE party = 'DEM') * 100.0 / sum(votes), 1) AS dem,
        round(sum(votes) FILTER (WHERE party = 'REP') * 100.0 / sum(votes), 1) AS rep
        FROM '{OUT}/county_results.parquet' WHERE top_of_ticket GROUP BY 1 ORDER BY 1""")
        .fetchall()
        .__repr__()
        .replace("), (", "),\n (")
    )


# ---------------------------------------------------------------- 5) registration


def build_registration(con):
    log("registration")
    parts = []
    for e in GENERALS:
        p = SRC / f"voter_stats_{e}.parquet"
        if not p.exists():
            z = fetch(e, f"voter_stats_{e}.zip", SRC / f"voter_stats_{e}.zip")
            if not z:
                continue
            _, _, cols = header(z)
            if "total_voters" not in cols:  # 2002 has no header; its columns are 2004's
                cols = [
                    "county_desc",
                    "precinct_abbrv",
                    "age",
                    "party_cd",
                    "race_code",
                    "ethnic_code",
                    "sex_code",
                    "total_voters",
                    "election_date",
                    "stats_type",
                    "update_date",
                ]
            else:
                cols = None
            stream(
                con,
                z,
                lambda c: (
                    f"""SELECT '{e}' AS election, upper(trim(county_desc)) AS county,
                {pick(c, "precinct_abbrv")} AS precinct, {pick(c, "vtd_abbrv")} AS vtd, {pick(c, "party_cd")} AS party,
                {pick(c, "race_code")} AS race_code, {pick(c, "ethnic_code")} AS ethnic_code,
                {pick(c, "sex_code")} AS sex_code, {pick(c, "age")} AS age_group, lower({pick(c, "stats_type")}) AS stats_type,
                try_cast(total_voters AS INT) AS voters FROM t"""
                ),
                p,
                columns=cols,
            )
            z.unlink()
        parts.append(f"'{p}'")
    con.sql(f"""COPY (SELECT * FROM read_parquet([{", ".join(parts)}], union_by_name=true)
        ) TO '{OUT}/registration_stats.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    log(
        con.sql(f"""SELECT election, count(DISTINCT county), sum(voters) FROM '{OUT}/registration_stats.parquet'
        WHERE stats_type IS NULL OR stats_type = 'voter' GROUP BY 1 ORDER BY 1""")
        .fetchall()
        .__repr__()
        .replace("), (", "),\n (")
    )
    con.sql(f"""COPY (SELECT ncid AS voter_id, upper(county_desc) AS county, voter_reg_num, status_cd, voter_status_desc,
        reason_cd, voter_status_reason_desc, registr_dt, party_cd, race_code, ethnic_code, gender_code, birth_year,
        res_city_desc AS city, zip_code AS zip, precinct_abbrv, precinct_desc, municipality_desc, ward_desc, vtd_abbrv,
        cong_dist_abbrv, nc_senate_abbrv, nc_house_abbrv, super_court_abbrv, judic_dist_abbrv, county_commiss_desc,
        school_dist_desc FROM '{SRC}/ncvoter.parquet'
        ) TO '{OUT}/registration_current.parquet' (FORMAT parquet, COMPRESSION zstd)""")


STEPS = {
    "sources": build_sources,
    "votes": build_votes,
    "wide": build_wide,
    "places": build_places,
    "results": build_results,
    "registration": build_registration,
}


def main(argv):
    OUT.mkdir(parents=True, exist_ok=True)
    steps = argv or list(STEPS)
    con = connect()
    for s in steps:
        t = dt.datetime.now()
        STEPS[s](con)
        log(f"{s}: {(dt.datetime.now() - t).seconds}s")


if __name__ == "__main__":
    main(sys.argv[1:])
