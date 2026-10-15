# Running it

Requires Python 3.11 or newer and nothing else; every script uses only the
standard library. You can run the scripts with `python3 script.py`, as
`./script.py`, or with `uv run script.py`.

## 1. Collect wait times

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
ago each site last changed its estimate. When a site's wait changes, the row
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
./visualize_waits.py FILE.csv --note "Sites closed early Saturday for weather."
```

| Option | Meaning |
|---|---|
| `CSV ...` | One or more monitor CSVs, combined (default `waits.csv`) |
| `-o FILE` | Output HTML (default `waits_report.html` next to the first CSV) |
| `--open` | Open the report in a browser |
| `--note TEXT` | Add a line to the report's Data notes (repeatable). Gaps in polling are listed automatically. |
| `--no-turnout` | Skip the State Board turnout data |
| `--election YYYYMMDD` | Election to get turnout for (default `20261103`) |

The report is one self-contained HTML file and works offline.

**Turnout.** Each run downloads the State Board's statewide absentee file into
`ncsbe_cache/`, but only when the State Board has posted a newer copy (about once
a day, early morning). The file grows over early voting; the 2024 General
Election's final file was about 200 MB. Each day's votes appear the following
morning. Delete `ncsbe_cache/` whenever you like.

**Site names.** The State Board and the county tracker name sites differently,
so the script matches them by distinctive words, including the town. Each run
prints every pairing and any State Board site it couldn't match. If a pairing
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
  --hide-scrollbars --window-size=1150,4000 --screenshot=docs/report.png \
  "file://$PWD/docs/index.html"
git add docs 2026-10-15_to_2026_10-31.csv && git commit -m "Update report"
```

Adjust the screenshot height if the page grows. After you push, GitHub Pages
publishes `docs/` to https://rtphokie.github.io/wakeearlyvotewait/ within a minute
or two.

## Development

```sh
uvx ruff check --fix *.py && uvx ruff format *.py
```
