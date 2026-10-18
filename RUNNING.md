# Running it

Requires Python 3.11 or newer; every script uses only the standard library.
The report's county turnout also needs `pdftotext` from poppler
(`brew install poppler`); without it the report uses State Board turnout only. You can run the scripts with `python3 script.py`, as
`./script.py`, or with `uv run script.py`.

## 1. Collect reported wait times

Check the tracker once:

```sh
./wake_wait_monitor.py --once
```

Poll every 5 minutes and save every reading:

```sh
./wake_wait_monitor.py --csv 2026-10-15_to_2026_10-31.csv --interval 300
```

| Option | Meaning |
|---|---|
| `--interval N` | Seconds between polls (default 300). Sites only update every 10–90 min, so faster isn't useful. |
| `--csv FILE` | Append each poll to FILE (created with a header if it doesn't exist) |
| `--once` | Poll once and exit |
| `--json` | Print JSON instead of a table |
| `--site TEXT` | Only show sites whose name contains TEXT |
| `--no-color` | No ANSI colors |

The table is sorted by city, then longest wait first. **Updated** shows how long
ago each site last changed its reported wait time. When a site's wait changes, the row
shows what it was before.

### Only during voting hours

The tracker keeps no history, so any time the monitor isn't running is lost.
`run_during_early_voting.py` starts the monitor 20 minutes before sites open and
stops it 20 minutes after they close. It restarts the monitor if it crashes and
exits after the last day of early voting. It reads the schedule from the county's
early voting page and falls back to the built-in 2026 schedule if that fails.
Arguments after `--` go to the monitor:

```sh
./run_during_early_voting.py -- --csv 2026-10-15_to_2026_10-31.csv --interval 300
./run_during_early_voting.py --pad 30 -- --csv 2026-10-15_to_2026_10-31.csv
```

Keep the machine awake while it runs. On a Mac:

```sh
caffeinate -i ./run_during_early_voting.py -- --csv 2026-10-15_to_2026_10-31.csv
```

The monitor and the wrapper stop if the terminal closes; use `tmux`/`screen`,
`nohup`, or launchd to keep them going.

## 2. Build the report

```sh
./visualize_waits.py 2026-10-15_to_2026_10-31.csv --open   # -> waits_report.html
./visualize_waits.py a.csv b.csv -o report.html             # combine several CSVs
```

| Option | Meaning |
|---|---|
| `CSV ...` | One or more monitor CSVs, combined (default `waits.csv`) |
| `-o FILE` | Output HTML (default `waits_report.html` next to the first CSV) |
| `--open` | Open the report in a browser |
| `--no-turnout` | Skip the county and State Board turnout data |
| `--election YYYYMMDD` | Election to get turnout for (default `20261103`) |

The report is one self-contained HTML file and works offline.

**Turnout.** Each run downloads two files into `ncsbe_cache/`, each only when
there's a newer copy:

- The county's [unofficial early voting turnout PDF](https://www.wake.gov/departments-government/board-elections/election-information/2026-general-election),
  refreshed each evening with votes per site per day. This is the main source,
  so each day's votes appear the same evening. The script reads the table on
  its first page with `pdftotext -layout`; if the county changes the layout, it
  prints a warning and falls back to the State Board.
- The State Board's statewide absentee file, refreshed about once a day, early
  morning. It's used for any day the county file doesn't have. The file grows
  over early voting; the 2024 General Election's final file was about 200 MB.

Each run prints both sources' totals for any day they both have. Delete
`ncsbe_cache/` whenever you like.

**Site names.** The turnout files and the county tracker name sites differently,
so the script matches them by distinctive words, including the town. Each run
prints every pairing and any turnout site it couldn't match. If a pairing
is wrong or missing, add it to `SITE_ALIASES` near the top of
`visualize_waits.py`:

```python
SITE_ALIASES = {"WC - WAKE COUNTY COMMONS BUILDING": "Wake County Commons Building"}
```

**Wait labels.** Waits are shown exactly as the tracker reports them. For
averages, ranges count as their midpoint (`15-30 Minutes` → 22.5) and open-ended
labels count as their lower bound (`2+ Hours` → 120). Hours are understood, so
labels like `1-2 Hours` work. Any label that isn't a time (e.g. `Closed`) shows
as gray in the report and is listed when the script runs.

## 3. Update the published report

```sh
./visualize_waits.py 2026-10-15_to_2026_10-31.csv -o docs/index.html
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
  --hide-scrollbars --window-size=1150,2700 --screenshot=docs/report.png \
  "file://$PWD/docs/index.html"
git add docs 2026-10-15_to_2026_10-31.csv && git commit -m "Update report"
```

Adjust the screenshot height if the page grows. After you push, GitHub Pages
publishes `docs/` to https://rtphokie.github.io/wakeearlyvotewait/ within a minute
or two.

### Automatically, every 10 minutes

`publish_report.sh` rebuilds the report and pushes it. It skips the run unless a
site posted a new report (or `visualize_waits.py` changed) since the last publish, retakes the screenshot once a day, and
commits only the CSV and `docs/`. It runs from launchd, so it catches up after the
Mac sleeps. After `END_DATE` (Nov 1) it unloads its own job.

Install it by saving this as
`~/Library/LaunchAgents/com.rtphokie.wakeearlyvotewait.publish.plist`, with the
paths changed to match your machine:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.rtphokie.wakeearlyvotewait.publish</string>
  <key>ProgramArguments</key>
  <array><string>/path/to/wakeearlyvotewait/publish_report.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHON</key><string>/path/to/python3.11</string>
  </dict>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/path/to/wakeearlyvotewait/logs/publish.log</string>
  <key>StandardErrorPath</key><string>/path/to/wakeearlyvotewait/logs/publish.log</string>
</dict>
</plist>
```

Then:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rtphokie.wakeearlyvotewait.publish.plist
launchctl kickstart -k gui/$(id -u)/com.rtphokie.wakeearlyvotewait.publish   # run now
tail -f logs/publish.log                                                      # watch it
launchctl bootout gui/$(id -u)/com.rtphokie.wakeearlyvotewait.publish         # stop it
```

Pushing uses your `gh` login (`gh auth login`), so no other credentials are needed.

## 4. Statewide report (2026_NC_Voting)

`nc_voting_report.py` builds `docs/2026_NC_Voting/index.html` from State Board
files in `ncsbe_cache/`. One-time setup downloads the earlier elections and
builds the history file (about 3 GB of downloads, 10 minutes):

```sh
B=https://s3.amazonaws.com/dl.ncsbe.gov
cd ncsbe_cache
for d in 2002_11_05 2004_11_02 2006_11_07 2008_11_04 2010_11_02 2012_11_06 \
         2014_11_04 2016_11_08 2018_11_06 2020_11_03 2022_11_08 2024_11_05; do
  curl -fO "$B/ENRS/$d/absentee_${d//_/}.zip"
done
curl -fO $B/data/ncvhis_Statewide.zip
curl -fO $B/data/ncvoter_Statewide.zip
curl -fo onestop_sites_20221108.pdf "$B/One-Stop_Early_Voting/2022/One-Stop%20Voting%20Sites%20for%20the%20November%208,%202022%20Election.pdf"
curl -fo onestop_sites_20241105.pdf "$B/One-Stop_Early_Voting/2024/Early-Voting-Site-List-2024-General.pdf"
curl -fo onestop_sites_20261103.pdf "$B/One-Stop_Early_Voting/2026/Early_Voting_Site_List_November_2026.pdf"
cd ..
./build_ballot_history.py      # -> ncsbe_cache/ballots_federal.tsv.gz
./build_site_crosswalk.py      # -> ncsbe_cache/onestop_site_crosswalk.json, onestop_site_changes.json
./build_site_results.py        # -> ncsbe_cache/onestop_site_results.json (also run by each report build)
```

Then build the report:

```sh
./nc_voting_report.py           # downloads a newer 2026 file if there is one; exits 3 if the report is current
./nc_voting_report.py --force --open
./nc_voting_report.py --no-fetch --force   # use the cached 2026 file
```

A build takes a few minutes, longer as the 2026 file grows. The page shows only
totals; the files it reads list individual voters and stay in `ncsbe_cache/`.

### Interim data sets

`build_interim.py` (needs `pip install duckdb`) turns the same downloads into
Parquet files in `ncsbe_cache/interim/`, for analysis beyond the report. It
downloads what else it needs from the State Board: absentee files for primaries
and municipal elections since 2016 (deleted once converted), polling place
lists since 2008, results and registration counts for each general.

```sh
./build_interim.py                         # everything, about 10 minutes
./build_interim.py sources votes places    # or some steps: sources votes wide places results registration
```

| File | One row per | Notes |
| --- | --- | --- |
| `voters.parquet` | voter | Current party, race, ethnicity, sex and birth year; from the latest ballot record for voters no longer in the voter file |
| `votes.parquet` | voter and election | Method (ONESTOP, MAIL, ELECTION_DAY), place, party at the time, ballot date |
| `voter_elections.parquet` | voter | One column per election (`e20241105`, ...): the `place_id` where they voted, `MAIL`, or the method when the place isn't known; empty when there's no record of a vote |
| `places.parquet` | voting place | Address and county, the elections it was an Election Day polling place or a one-stop site, and `onestop_only` |
| `place_elections.parquet` | place and election | How it was used, with its precincts or site name |
| `county_results.parquet` | county, general, contest and party | Votes and share for every partisan contest since 2002; `top_of_ticket` marks President, or US Senate in midterms (US House, all districts, in 2006 and 2018) |
| `registration_stats.parquet` | general and group | Registered voters at each general since 2002 by county, precinct, party, race, ethnicity, sex and the State Board's age groups |
| `registration_current.parquet` | voter | Current registration status, county, precinct and districts |

What the sources cover:
- **Voters** are keyed by `ncid`. Ballots before 2014 carry only county and
  registration number. They're linked to an `ncid` where any file has both
  (53% of 2002 ballots, 77% of 2012's), and otherwise keyed `COUNTY:regnum`.
- **Generals from 2002 to 2014** come from the absentee files only, so they
  include mail and one-stop ballots but no Election Day voters. Every election
  since November 2016 comes from the voter history file, which covers all methods.
- **One-stop sites** aren't recorded before 2014. 2014 to 2020 sites are named but
  have no address list, so they're matched by name to later sites or to polling
  places. Those that don't match keep a name-only `place_id` (`WAKE|NAME:...`) and
  an unknown `onestop_only`.
- **Election Day places** are each voter's precinct polling place for that election.

No names, street addresses or phone numbers are copied into these files. The
large zips are streamed through a pipe rather than extracted, and DuckDB's
memory and temporary files are capped (10 GB and 6 GB), because the disk is
nearly full.

### Automatically, as soon as the State Board posts a new file

`publish_nc_report.sh` checks for a newer State Board file, rebuilds the report
when there is one (or when the report code changed since the last publish), and
commits and pushes only `docs/2026_NC_Voting/`. It shares `publish_report.sh`'s
lock, so the two never push at the same time. After `END_DATE` (Nov 30, after
the canvass) it unloads its own job. Install it like the wait-time job, as
`~/Library/LaunchAgents/com.rtphokie.wakeearlyvotewait.publish-nc.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.rtphokie.wakeearlyvotewait.publish-nc</string>
  <key>ProgramArguments</key>
  <array><string>/path/to/wakeearlyvotewait/publish_nc_report.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHON</key><string>/path/to/python3.11</string>
  </dict>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/path/to/wakeearlyvotewait/logs/publish_nc.log</string>
  <key>StandardErrorPath</key><string>/path/to/wakeearlyvotewait/logs/publish_nc.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rtphokie.wakeearlyvotewait.publish-nc.plist
tail -f logs/publish_nc.log
```

## Logs

Each script logs to `logs/`, which isn't committed:

| Log | What's in it |
|---|---|
| `logs/monitor.log` | Monitor start/stop, every poll (`poll ok: 18 sites; changed: Cary Senior Center 15-30 Minutes -> 30-60 Minutes`), and fetch errors |
| `logs/scheduler.log` | `run_during_early_voting.py` starting, stopping and restarting the monitor around voting hours |
| `logs/publish.log` | Every publish run: skipped (no new readings), published (with the commit), or failed (with the line) |
| `logs/publish_nc.log` | Every statewide-report run: skipped (no new State Board file), published, or failed |

Use `--log FILE` to log somewhere else, or `--log ''` to turn logging off, for the
monitor and the scheduler.

```sh
tail -f logs/*.log
```

## Development

```sh
uvx ruff check --fix *.py && uvx ruff format *.py
```
