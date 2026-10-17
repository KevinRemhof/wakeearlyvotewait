#!/bin/bash
# Rebuild the statewide 2026 NC early voting report and publish it to GitHub Pages
# (docs/2026_NC_Voting/, served at https://rtphokie.github.io/wakeearlyvotewait/2026_NC_Voting/).
#
# Meant to run every 30 minutes from launchd (see RUNNING.md). Each run:
#   - checks the State Board for a newer statewide absentee file (posted early each morning)
#   - does nothing unless there is one, or the report code changed since the last publish
#   - rebuilds docs/2026_NC_Voting/index.html (takes a few minutes)
#   - commits only docs/2026_NC_Voting/, then pushes; Pages updates a minute later
#   - logs what it did to logs/publish_nc.log
# It shares publish_report.sh's lock, so the two never commit or push at the same time.
# After END_DATE it unloads its own launchd job and exits.
#
# Settings come from the environment (set them in the launchd plist):
#   PYTHON    Python 3.11+ interpreter      (default: python3)
#   GH        GitHub CLI, used for push auth (default: gh)
#   END_DATE  last day to publish           (default: 2026-11-30, after the canvass)
#   LABEL     launchd label to unload after END_DATE

set -euo pipefail
cd "$(dirname "$0")"

PYTHON=${PYTHON:-python3}
GH=${GH:-gh}
END_DATE=${END_DATE:-2026-11-30}
LABEL=${LABEL:-com.rtphokie.wakeearlyvotewait.publish-nc}
OUT_DIR=docs/2026_NC_Voting
CODE=(nc_voting_report.py analyze_2026_voters.py build_site_crosswalk.py build_ballot_history.py geocode_sites.py build_site_results.py)

mkdir -p logs
if [[ -t 1 ]]; then
    exec > >(tee -a logs/publish_nc.log) 2>&1
else
    exec >>logs/publish_nc.log 2>&1
fi
log() { echo "[publish-nc $(date '+%Y-%m-%d %H:%M:%S')] $*"; }
trap 'log "FAILED at line $LINENO (exit $?)"' ERR

if [[ "$(date +%F)" > "$END_DATE" ]]; then
    log "past $END_DATE; unloading $LABEL"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    exit 0
fi

LOCK=.publish.lock
if [[ -d "$LOCK" ]] && [[ -n "$(find "$LOCK" -maxdepth 0 -mmin +30)" ]]; then
    log "removing stale lock from a run that didn't finish"
    rmdir "$LOCK"
fi
if ! mkdir "$LOCK" 2>/dev/null; then
    log "another publish is running; skipping"
    exit 0
fi
trap 'rmdir "$LOCK"' EXIT

# Rebuild regardless of the State Board file when the report code changed since the last publish.
force=()
if [[ "$(git log -1 --format=%ct -- "${CODE[@]}")" -gt "$(git log -1 --format=%ct -- "$OUT_DIR" 2>/dev/null || echo 0)" ]]; then
    force=(--force)
    log "report code changed since the last publish; rebuilding"
fi

status=0
"$PYTHON" nc_voting_report.py -o "$OUT_DIR/index.html" "${force[@]}" || status=$?
if [[ $status -eq 3 ]]; then
    log "no new State Board file since the last publish; nothing to publish"
    exit 0
elif [[ $status -ne 0 ]]; then
    log "report build failed (exit $status)"
    exit "$status"
fi

git add -- "$OUT_DIR"
if git diff --cached --quiet -- "$OUT_DIR"; then
    log "report unchanged; nothing to commit"
    exit 0
fi
git commit -q -m "Update NC voting report ($(date '+%a %b %-d %-I:%M %p'))" -- "$OUT_DIR"

push() { git -c credential.helper= -c credential.helper="!$GH auth git-credential" push -q origin main; }
if ! push; then
    log "push rejected; rebasing onto origin/main and retrying"
    git -c credential.helper= -c credential.helper="!$GH auth git-credential" pull -q --rebase --autostash origin main
    push
fi
log "published $(git rev-parse --short HEAD)"
