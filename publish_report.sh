#!/bin/bash
# Rebuild the report from the CSV and publish it to GitHub Pages.
#
# Meant to run every 20 minutes from launchd (see RUNNING.md). Each run:
#   - does nothing if neither the CSV nor visualize_waits.py changed since the last publish
#   - rebuilds docs/index.html (with State Board turnout)
#   - retakes docs/report.png once a day, to keep the repo from bloating
#   - commits only the CSV and docs/, then pushes; Pages updates a minute later
#   - logs what it did to logs/publish.log
# After END_DATE it unloads its own launchd job and exits.
#
# Settings come from the environment (set them in the launchd plist):
#   PYTHON    Python 3.11+ interpreter      (default: python3)
#   GH        GitHub CLI, used for push auth (default: gh)
#   CHROME    Chrome binary for screenshots (default: Google Chrome in /Applications)
#   CSV       data file                     (default: 2026-10-15_to_2026_10-31.csv)
#   END_DATE  last day to publish           (default: 2026-11-01)
#   LABEL     launchd label to unload after END_DATE

set -euo pipefail
cd "$(dirname "$0")"

PYTHON=${PYTHON:-python3}
GH=${GH:-gh}
CHROME=${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}
CSV=${CSV:-2026-10-15_to_2026_10-31.csv}
END_DATE=${END_DATE:-2026-11-01}
LABEL=${LABEL:-com.rtphokie.wakeearlyvotewait.publish}
SHOT_HEIGHT=3800 # tall enough for the whole page; any extra is page background

# Log every run to logs/publish.log (also shown on screen when run by hand).
mkdir -p logs
if [[ -t 1 ]]; then
    exec > >(tee -a logs/publish.log) 2>&1
else
    exec >>logs/publish.log 2>&1
fi
log() { echo "[publish $(date '+%Y-%m-%d %H:%M:%S')] $*"; }
trap 'log "FAILED at line $LINENO (exit $?)"' ERR

if [[ "$(date +%F)" > "$END_DATE" ]]; then
    log "past $END_DATE; unloading $LABEL"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    exit 0
fi

# One run at a time (a slow download or push could overlap the next run).
LOCK=.publish.lock
if [[ -d "$LOCK" ]] && [[ -n "$(find "$LOCK" -maxdepth 0 -mmin +30)" ]]; then
    log "removing stale lock from a run that didn't finish"
    rmdir "$LOCK"
fi
if ! mkdir "$LOCK" 2>/dev/null; then
    log "previous run still going; skipping"
    exit 0
fi
trap 'rmdir "$LOCK"' EXIT

# Publish when there are new readings, or the report code changed since the last publish.
code_changed() {
    [[ "$(git log -1 --format=%ct -- visualize_waits.py)" -gt "$(git log -1 --format=%ct -- docs/index.html)" ]]
}
if git diff --quiet -- "$CSV" && ! code_changed; then
    log "no new readings in $CSV; nothing to publish"
    exit 0
fi

"$PYTHON" visualize_waits.py "$CSV" -o docs/index.html
paths=("$CSV" docs/index.html)

if [[ "$(git log -1 --format=%cs -- docs/report.png)" != "$(date +%F)" ]]; then
    "$CHROME" --headless=new --disable-gpu --hide-scrollbars \
        --blink-settings=preferredColorScheme=1 --window-size=1150,$SHOT_HEIGHT \
        --screenshot=docs/report.png "file://$PWD/docs/index.html" 2>/dev/null
    paths+=(docs/report.png)
    log "updated screenshot"
fi

git add -- "${paths[@]}"
# Commit only these paths, leaving anything else staged or edited alone.
git commit -q -m "Update report ($(date '+%a %b %-d %-I:%M %p'))" -- "${paths[@]}"

push() { git -c credential.helper= -c credential.helper="!$GH auth git-credential" push -q origin main; }
if ! push; then
    log "push rejected; rebasing onto origin/main and retrying"
    git -c credential.helper= -c credential.helper="!$GH auth git-credential" pull -q --rebase --autostash origin main
    push
fi
log "published $(git rev-parse --short HEAD)"
