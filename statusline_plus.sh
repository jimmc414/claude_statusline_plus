#!/bin/bash
# statusline_plus.sh - run each claude_statusline_plus segment installed next to
# this file and join what they print with " · ".
#
#     cache warm 31m · 5h 12% · 7d 41%
#
# The installer points statusLine.command here when you have no status line of
# your own. With one of your own, call the segments from it instead (see the
# README). Segments that have nothing to say print nothing and are skipped.

input=$(cat)
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
out=""
for segment in cache_warm.sh usage_forecast.sh; do
    [ -f "$here/$segment" ] || continue
    text=$(printf '%s' "$input" | bash "$here/$segment" 2>/dev/null)
    [ -n "$text" ] && out="${out:+$out · }$text"
done
[ -n "$out" ] && printf '%s\n' "$out"
exit 0
