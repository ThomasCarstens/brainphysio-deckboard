#!/usr/bin/env bash
# deckboard.sh — open the Deckboard window from the desktop/dock.
# Terminal=false in the .desktop entry means a traceback would go nowhere, so a failure is
# surfaced as a desktop notification and the log is kept for the next time someone asks
# "it didn't open".
#
# REPO is the brainphysio checkout to read. An installed copy has it written in (see
# README.md, "The dock launcher"); run from inside the submodule, it is the checkout the
# submodule sits in.
REPO=${DECKBOARD_REPO:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)}
LOG=${XDG_CACHE_HOME:-$HOME/.cache}/deckboard-launch.log

fail() {
    notify-send -i "$HOME/.local/share/icons/brain.svg" "Deckboard failed to start" "$1" 2>/dev/null
    echo "$1" >&2
    exit 1
}

# Deckboard is a submodule of human_m123 only, so on any other branch the folder is empty
# or absent — which is a checkout to change, not a crash to read a traceback for.
[ -f "$REPO/deckboard/deckboard.py" ] ||
    fail "$REPO has no deckboard/ checked out. On human_m123: git submodule update --init deckboard"

cd "$REPO" || exit 1
/usr/bin/python3 deckboard/deckboard.py "$@" >"$LOG" 2>&1 || fail "$(tail -n 3 "$LOG")"
