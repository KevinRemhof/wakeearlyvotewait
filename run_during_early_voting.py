#!/usr/bin/env python3
"""Keep wake_wait_monitor.py running only while early voting sites are open.

The monitor is started PAD minutes (default 20) before the earliest opening
time of the day and stopped PAD minutes after the latest closing time. If it
crashes inside the window it is restarted. The wrapper exits after the final
day of early voting.

The schedule is scraped from the Wake County early voting page (refreshed
daily); if that fails, the built-in 2026 General Election schedule is used.

Any arguments after "--" are passed to the monitor. Example:
    python3 run_during_early_voting.py -- --csv waits.csv --interval 300
"""

import argparse
import html
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
SCHEDULE_URL = "https://wakevotesearly.com"
MONITOR = Path(__file__).with_name("wake_wait_monitor.py")
CHECK_SECONDS = 30

# Fallback: 2026 General Election early voting hours (same for every site).
_WEEKDAY, _SAT, _SUN = (
    ("8:00 a.m.", "7:30 p.m."),
    ("8:00 a.m.", "3:00 p.m."),
    ("1:00 p.m.", "6:00 p.m."),
)
FALLBACK_SCHEDULE = {
    date(2026, 10, d): hours
    for d, hours in {
        15: _WEEKDAY,
        16: _WEEKDAY,
        17: _SAT,
        18: _SUN,
        19: _WEEKDAY,
        20: _WEEKDAY,
        21: _WEEKDAY,
        22: _WEEKDAY,
        23: _WEEKDAY,
        24: _SAT,
        25: _SUN,
        26: _WEEKDAY,
        27: _WEEKDAY,
        28: _WEEKDAY,
        29: _WEEKDAY,
        30: _WEEKDAY,
        31: _SAT,
    }.items()
}


def log(msg):
    print(
        f"[wrapper {datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}",
        file=sys.stderr,
        flush=True,
    )


def parse_clock(text):
    """'8:00 a.m.' -> (8, 0)"""
    m = re.match(r"(\d{1,2}):(\d{2})\s*([ap])\.?m\.?", text.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(f"bad time: {text!r}")
    hour, minute, ampm = int(m.group(1)) % 12, int(m.group(2)), m.group(3).lower()
    return hour + (12 if ampm == "p" else 0), minute


def fetch_schedule():
    """Scrape {date: (open_text, close_text)} from the county page's schedule table.

    The table lists one row per day; when it gives multiple rows for a date
    (e.g. sites with different hours) we keep the earliest open and latest close.
    """
    req = urllib.request.Request(SCHEDULE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        page = resp.read().decode("utf-8")
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    text = re.sub(r"\s+", " ", text)
    row = re.compile(
        r"([A-Z][a-z]+ \d{1,2}, \d{4}) [A-Z][a-z]+day "
        r"(\d{1,2}:\d{2} [ap]\.m\.) (\d{1,2}:\d{2} [ap]\.m\.)"
    )
    schedule = {}
    for d_text, open_text, close_text in row.findall(text):
        d = datetime.strptime(d_text, "%B %d, %Y").date()
        if d in schedule:
            old_open, old_close = schedule[d]
            open_text = min(old_open, open_text, key=parse_clock)
            close_text = max(old_close, close_text, key=parse_clock)
        schedule[d] = (open_text, close_text)
    if not schedule:
        raise RuntimeError("no schedule rows found on page")
    return schedule


def windows(schedule, pad):
    """Return sorted list of (start, end) aware datetimes, padded by `pad`."""
    out = []
    for d, (open_text, close_text) in schedule.items():
        oh, om = parse_clock(open_text)
        ch, cm = parse_clock(close_text)
        start = datetime(d.year, d.month, d.day, oh, om, tzinfo=TZ) - pad
        end = datetime(d.year, d.month, d.day, ch, cm, tzinfo=TZ) + pad
        out.append((start, end))
    return sorted(out)


def load_windows(pad):
    try:
        schedule = fetch_schedule()
        log(f"loaded schedule from county page ({len(schedule)} days)")
    except (
        OSError,
        ValueError,
        RuntimeError,
    ) as e:  # network, HTTP or page-layout failure
        log(f"could not load schedule from county page ({e}); using built-in schedule")
        schedule = FALLBACK_SCHEDULE
    return windows(schedule, pad)


def stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    signal.signal(
        signal.SIGTERM, _raise_interrupt
    )  # so `kill`/launchd also stop the monitor
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--pad",
        type=int,
        default=20,
        help="minutes before open / after close (default 20)",
    )
    p.add_argument(
        "monitor_args",
        nargs=argparse.REMAINDER,
        help="arguments passed to the monitor (after --)",
    )
    args = p.parse_args()
    monitor_args = (
        args.monitor_args[1:] if args.monitor_args[:1] == ["--"] else args.monitor_args
    )
    pad = timedelta(minutes=args.pad)

    wins = load_windows(pad)
    loaded_on = datetime.now(TZ).date()
    proc = None
    announced = None
    try:
        while True:
            now = datetime.now(TZ)
            if (
                now.date() != loaded_on and proc is None
            ):  # refresh schedule daily while idle
                wins, loaded_on = load_windows(pad), now.date()

            current = next(((s, e) for s, e in wins if s <= now < e), None)
            upcoming = next(((s, e) for s, e in wins if s > now), None)

            if current:
                if proc is None or proc.poll() is not None:
                    if proc is not None:
                        log(f"monitor exited with code {proc.returncode}; restarting")
                    log(
                        f"starting monitor (window ends {current[1]:%a %b %d %I:%M %p})"
                    )
                    proc = subprocess.Popen(
                        [sys.executable, "-u", str(MONITOR), *monitor_args]
                    )
            else:
                if proc is not None:
                    log("outside voting hours; stopping monitor")
                    stop(proc)
                    proc = None
                if upcoming is None:
                    log("early voting is over; exiting")
                    return
                if announced != upcoming:
                    log(f"waiting; next start {upcoming[0]:%a %b %d %I:%M %p}")
                    announced = upcoming
            time.sleep(CHECK_SECONDS)
    except KeyboardInterrupt:
        log("stopped; shutting down monitor")
    finally:
        stop(proc)


if __name__ == "__main__":
    main()
