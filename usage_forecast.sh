#!/bin/bash
# usage_forecast.sh - Claude Code status line segment: will your usage limits
# last until they reset, and which session is burning them?
#
# Reads the status line JSON payload on stdin and prints a single segment:
#
#     5h 12% · 7d 41%                          on track
#     5h 48% 1.3× · 7d 41%                     (yellow) burning faster than the window lasts
#     5h 53% 3.9× cap 15:36 (resets 19:20)     (red) at this pace the limit runs out first
#     ... · top: refactor auth 57%             the session doing most of the burning
#
# Prints nothing when the payload has no rate_limits: API-key sessions, and
# subscription sessions before their first response.
#
# Pace is the burn rate as a multiple of the rate that uses exactly 100% over a
# full window: 1.0× lasts the window, 4.0× empties it in a quarter of it. The rate
# comes from readings this script records whenever the meter rises. Every session
# on the machine shares them, so they follow the whole account, not one session.
# It is measured over the last 30 minutes (5-hour window) or 6 hours (weekly);
# until that much history exists, it is the average since the window opened.
#
# "top:" names the session with the largest share of recent token spend, read from
# the transcripts under ~/.claude/projects and weighted by list price. It shows
# while a limit is on course to run out and more than one session is spending.
# The scan runs in the background and is cached, so the status line never waits.
#
# Environment:
#   USAGE_FORECAST_STYLE        short (default) | long (full sentences)
#   USAGE_FORECAST_TOP          warn (default) | always | never
#   USAGE_FORECAST_WINDOW       seconds of spend the "top" share covers (default 1800)
#   USAGE_FORECAST_CACHE_DIR    where readings and scan results live
#                               (default ${XDG_CACHE_HOME:-~/.cache}/claude-statusline-plus)
#   USAGE_FORECAST_PROJECTS     transcripts to scan (default ~/.claude/projects,
#                               or $CLAUDE_CONFIG_DIR/projects)
#   USAGE_FORECAST_TAIL_BYTES   bytes read from the end of a large transcript (default 16 MiB)
#   USAGE_FORECAST_NOW          override current epoch seconds (for tests)
#   USAGE_FORECAST_SYNC         1 = scan transcripts inline, not in the background (for tests)
#   NO_COLOR                    disable ANSI colors

command -v jq >/dev/null 2>&1 || { cat >/dev/null; exit 0; }

style="${USAGE_FORECAST_STYLE:-short}"
top_mode="${USAGE_FORECAST_TOP:-warn}"
window="${USAGE_FORECAST_WINDOW:-1800}"
cache_dir="${USAGE_FORECAST_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/claude-statusline-plus}"
projects="${USAGE_FORECAST_PROJECTS:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects}"
tail_bytes="${USAGE_FORECAST_TAIL_BYTES:-16777216}"
now="${USAGE_FORECAST_NOW:-}"
# Digit caps keep every value inside bash's 64-bit arithmetic.
[[ "$window" =~ ^[0-9]{1,7}$ ]] && [ "$window" -ge 60 ] || window=1800
[[ "$tail_bytes" =~ ^[0-9]{1,11}$ ]] && [ "$tail_bytes" -ge 1024 ] || tail_bytes=16777216
[[ "$now" =~ ^[0-9]{1,12}$ ]] || now=$(date +%s)
case "$top_mode" in warn|always|never) ;; *) top_mode=warn ;; esac
case "$style" in short|long) ;; *) style=short ;; esac
projects="${projects%/}"
samples="$cache_dir/usage_samples.tsv"
fleet="$cache_dir/fleet.tsv"
FLEET_EVERY=60      # rescan transcripts at most this often (seconds)
FLEET_MAX_AGE=300   # never show a scan older than this

# =============================================================================
# Transcript scan: who is spending. Runs in the background (or inline for tests)
# and writes $fleet: a "#<TAB>computed_at<TAB>window" header, then one line per
# session, "session_id<TAB>list-price dollars<TAB>name", biggest spender first.
# =============================================================================

# One transcript line in, at most one TSV line out:
#   file U message_id dollars    an API call inside the window
#   file N name                  the session's name (--name or /rename)
#   file T title                 the session's AI-generated title
# -R + fromjson? skips the partial first line of a tail instead of failing.
_scan='
def ts: sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601;   # jq 1.7 rejects fractional seconds
def n: (numbers // 0);
# List prices in dollars per million tokens: [input, output, cache read]. Cache
# writes cost 1.25x input (5-minute tier) or 2x (1-hour tier).
def price: (strings // "") as $m
  | if   ($m | test("fable-5-1|mythos-5-1")) then [10, 50, 0.25]
    elif ($m | test("fable|mythos"))        then [10, 50, 1]
    elif ($m | test("opus-5-5"))            then [4, 20, 0.2]
    elif ($m | test("opus"))                then [5, 25, 0.5]
    elif ($m | test("sonnet-5"))            then [2, 10, 0.2]
    elif ($m | test("sonnet"))              then [3, 15, 0.3]
    elif ($m | test("haiku"))               then [1, 5, 0.1]
    else [5, 25, 0.5] end;
def dollars: .message as $m | ($m.usage) as $u | ($m.model | price) as $p
  | (($u.cache_creation | objects) // {}) as $c
  | ($c.ephemeral_1h_input_tokens | n) as $w1
  | ((($u.cache_creation_input_tokens | n) - $w1) | if . < 0 then 0 else . end) as $w5
  | ($p[0] * (($u.input_tokens | n) + 1.25 * $w5 + 2 * $w1)
     + $p[1] * ($u.output_tokens | n) + $p[2] * ($u.cache_read_input_tokens | n)) / 1000000;
inputs | fromjson? | objects
| (if $f != "" then $f else input_filename end) as $file   # jq 1.7 names stdin "<stdin>"
| if .type == "assistant" and (.message | type) == "object" and (.message.usage | type) == "object"
     and .message.model != "<synthetic>" and (.isApiErrorMessage | not) then
    (try (.timestamp | ts) catch null) as $t
    | if $t != null and $t >= $cutoff
      then [$file, "U", ((.message.id // .requestId // .uuid // "") | tostring), (dollars | tostring)] | @tsv
      else empty end
  elif .type == "agent-name" and (.agentName | type) == "string" then [$file, "N", .agentName] | @tsv
  elif .type == "ai-title" and (.aiTitle | type) == "string" then [$file, "T", .aiTitle] | @tsv
  else empty end'

# Sum each session's calls, once per message id: an API call is written as one
# line per content block, all carrying the same usage. Subagent and workflow
# transcripts (<project>/<session>/...) count toward their parent session; names
# come only from the session's own transcript (<project>/<session>.jsonl).
_aggregate='
BEGIN { FS = OFS = "\t"; n = length(root) }
function sid_of(path,   rel, parts, k, s) {
    if (substr(path, 1, n + 1) != root "/") return ""
    rel = substr(path, n + 2)
    k = split(rel, parts, "/")
    if (k < 2) return ""
    pj[path] = parts[1]
    if (k == 2) { s = parts[2]; sub(/\.jsonl$/, "", s); ismain[path] = 1; return s }
    return parts[2]
}
$2 == "U" && NF >= 4 {
    key = ($3 == "") ? "#" NR : $3
    if (key in seen) next
    seen[key] = 1
    s = sid_of($1); if (s == "") next
    cost[s] += $4; proj[s] = pj[$1]
    next
}
($2 == "N" || $2 == "T") && NF >= 3 {
    s = sid_of($1); if (s == "" || !ismain[$1]) next
    if ($2 == "N") name[s] = $3; else title[s] = $3
}
END {
    for (s in cost) if (cost[s] > 0)
        print s, proj[s], cost[s], ((s in name) ? name[s] : ((s in title) ? title[s] : "-"))
}'

# title_of <main transcript> <session id>: the session's name, else its AI title,
# from anywhere in the file. Cached for 10 minutes: a big transcript takes a moment.
title_of() {
    local main=$1 sid=$2 cached="" t=""
    case "$sid" in *[!A-Za-z0-9._-]*|"") ;; *) cached="$cache_dir/titles/$sid" ;; esac
    if [ -n "$cached" ] && [ -f "$cached" ] && [ -z "$(find "$cached" -mmin +10 2>/dev/null)" ]; then
        cat "$cached" 2>/dev/null; return 0
    fi
    if [ -f "$main" ]; then
        t=$(LC_ALL=C grep -a -o -E '"agentName"[[:space:]]*:[[:space:]]*"([^"\\]|\\.)*"' "$main" 2>/dev/null | tail -n 1)
        [ -n "$t" ] || t=$(LC_ALL=C grep -a -o -E '"aiTitle"[[:space:]]*:[[:space:]]*"([^"\\]|\\.)*"' "$main" 2>/dev/null | tail -n 1)
        [ -n "$t" ] && t=$(printf '{%s}' "$t" | jq -r '.[] | gsub("[[:cntrl:]]"; " ")' 2>/dev/null)
    fi
    [ -n "$cached" ] && mkdir -p "$cache_dir/titles" 2>/dev/null && printf '%s' "$t" >"$cached" 2>/dev/null
    printf '%s' "$t"
}

refresh_fleet() {
    [ -d "$projects" ] || return 0
    mkdir -p "$cache_dir" 2>/dev/null || return 0
    _lock="$cache_dir/fleet.lock"
    if ! mkdir "$_lock" 2>/dev/null; then
        # A scan that died leaves its lock behind; take it over after 2 minutes.
        [ -n "$(find "$_lock" -maxdepth 0 -mmin +2 2>/dev/null)" ] || return 0
        rm -rf "$_lock" && mkdir "$_lock" 2>/dev/null || return 0
    fi
    trap 'rm -rf "$_lock"' EXIT
    local minutes=$(( window / 60 + 1 )) cutoff=$(( now - window ))
    local small_kb=$(( tail_bytes / 1024 )) tmp="$fleet.$$"
    [ "$small_kb" -gt 2048 ] && small_kb=2048
    {
        printf '#\t%s\t%s\n' "$now" "$window"
        {
            # Small transcripts (most subagents) go through one jq, whole.
            find "$projects" -type f -name '*.jsonl' ! -name 'journal.jsonl' -mmin -"$minutes" \
                -size -"$small_kb"k -print0 2>/dev/null \
                | xargs -0 jq -nrR --arg f "" --argjson cutoff "$cutoff" "$_scan" 2>/dev/null
            # Large ones: only the tail, and only the line types that matter.
            find "$projects" -type f -name '*.jsonl' ! -name 'journal.jsonl' -mmin -"$minutes" \
                ! -size -"$small_kb"k -print0 2>/dev/null \
                | while IFS= read -r -d '' f; do
                    tail -c "$tail_bytes" "$f" 2>/dev/null \
                        | LC_ALL=C grep -a -E '"type"[[:space:]]*:[[:space:]]*"(assistant|agent-name|ai-title)"' 2>/dev/null \
                        | jq -nrR --arg f "$f" --argjson cutoff "$cutoff" "$_scan" 2>/dev/null
                  done
        } | awk -v root="$projects" "$_aggregate" \
          | while IFS=$'\t' read -r sid proj cost label; do
                [ "$label" = "-" ] && label=$(title_of "$projects/$proj/$sid.jsonl" "$sid")
                printf '%s\t%s\t%s\n' "$sid" "$cost" "$label"
            done \
          | sort -t "$(printf '\t')" -k2,2gr
    } >"$tmp" 2>/dev/null && mv "$tmp" "$fleet" 2>/dev/null
    rm -f "$tmp"
    find "$cache_dir/titles" -type f -mmin +1440 -exec rm -f {} + 2>/dev/null
    rm -rf "$_lock"
    trap - EXIT
}

if [ "${1:-}" = "--refresh-fleet" ]; then
    refresh_fleet
    exit 0
fi

# =============================================================================
# Render: one jq call over the payload, the recent readings and the last scan.
# Prints three lines: a reading to record (or nothing), "refresh" when the scan
# should be redone, and the segment itself.
# =============================================================================
_render='
def num: if type == "number" and (isinfinite | not) and (isnan | not) then . else null end;
def c($code): if $color == "1" then "\u001b[\($code)m" else "" end;
def lt($f): floor | (try strflocaltime($f) catch strftime($f));
def at($len): if $len > 18000 then lt("%a %H:%M") elif $len == 0 then lt("%b %d") else lt("%H:%M") end;
def pct: ((. + 0.5) | floor) as $p | (if . < 100 and $p >= 100 then 99 else $p end | tostring) + "%";
def x: (if . >= 10 then ((. + 0.5) | floor) else ((. * 10 + 0.5) | floor) / 10 end | tostring)
       | (if test("^[0-9]+$") and (tonumber < 10) then . + ".0" else . end) + "×";
def clean: gsub("[[:cntrl:]]"; "");
def trunc($n): if length > $n then .[0:$n - 1] + "…" else . end;

# A window from the payload, or null when it is missing, malformed, already past
# its reset, or claims to reset further out than a window lasts.
def win($o; $len):
  if ($o | type) != "object" then null else
    ($o.used_percentage | num) as $u | ($o.resets_at | num) as $r
    | if $u == null or $r == null or $u < 0 or $u > 1000 then null
      elif $r <= $now then null
      elif $len > 0 and $r > $now + $len + 3600 then null
      else {u: $u, r: ($r | floor), len: $len} end
  end;

# Readings: epoch, 5h used, 5h reset, 7d used, 7d reset ("-" when not recorded).
def rows: [ $samples | split("\n")[] | split("\t") | select(length == 5)
            | map(if . == "-" or . == "" then null else (tonumber? // null) end)
            | select(.[0] != null) ];

# True when this payload raises the highest reading recorded for its window.
# Usage never falls inside a window, so a lower value is a stale session.
def newer($w; $ui; $ri; $rows):
  $w != null and (([$rows[] | select(.[$ri] == $w.r) | .[$ui] | numbers] | max) as $m
                  | ($m == null or $w.u > $m));

def analyze($w; $ui; $ri; $rows):
  if $w == null then null
  elif $w.len == 0 then
    {u: $w.u, r: $w.r, len: 0, pace: null, eta: null, capped: ($w.u >= 100),
     red: ($w.u >= 100), yellow: ($w.u >= 80)}
  else
    [$rows[] | select(.[$ri] == $w.r and (.[$ui] | numbers) != null)] as $ws
    | ([$w.u, ([$ws[] | .[$ui]] | max // 0)] | max) as $u
    | $w.len as $L
    | (if $L == 18000 then 1800 else 21600 end) as $look
    | (if $L == 18000 then 600 else 3600 end) as $span
    # Usage at the start of the look-back: the last reading before it (flat until
    # the next one), else the oldest reading inside it.
    | ([$ws[] | select(.[0] < $now - $look)] | max_by(.[0])) as $prev
    | (if $prev != null then [$now - $look, $prev[$ui]]
       else ([$ws[] | select(.[0] <= $now)] | min_by(.[0])) as $o
            | if $o == null then null else [$o[0], $o[$ui]] end
       end) as $ref
    | (if $ref != null and ($now - $ref[0]) >= $span
       then ([$u - $ref[1], 0] | max) / ($now - $ref[0]) else null end) as $recent
    | ($now - ($w.r - $L)) as $el
    | (if $el >= ([$L * 0.05, 600] | max) and $el <= $L then $u / $el else null end) as $avg
    | ($recent // $avg) as $rate
    | (if $rate == null then null else $rate * $L / 100 end) as $pace
    | (if $u >= 100 then $now
       elif $rate != null and $rate > 0 then $now + (100 - $u) / $rate
       else null end) as $eta
    | {u: $u, r: $w.r, len: $L, pace: $pace, eta: $eta, capped: ($u >= 100),
       red: ($u >= 100 or ($eta != null and $eta < $w.r)),
       yellow: ($pace != null and $pace >= 1)}
  end;

def short($a; $label):
  if $a == null then empty else
    (c("2") + $label + c("0") + " ") as $head
    | (c("2") + " (resets " + ($a.r | at($a.len)) + ")" + c("0")) as $resets
    | if $a.len == 0 then
        $head + (if $a.red then c("38;5;196") elif $a.yellow then c("38;5;220") else "" end)
        + ((($a.u + 0.5) | floor | tostring) + "%") + (if $a.red or $a.yellow then c("0") else "" end)
        + (if $a.red then $resets else "" end)
      elif $a.capped then $head + c("38;5;196") + "100% capped" + c("0") + $resets
      elif $a.red then
        $head + c("38;5;196") + ($a.u | pct) + " " + ($a.pace | x) + " cap " + ($a.eta | at($a.len))
        + c("0") + $resets
      elif $a.yellow then $head + c("38;5;220") + ($a.u | pct) + " " + ($a.pace | x) + c("0")
      else $head + ($a.u | pct) end
  end;

def long($a; $name):
  if $a == null then empty
  elif $a.len == 0 then
    (if $a.red then c("38;5;196") elif $a.yellow then c("38;5;220") else "" end)
    + "\($name) \((($a.u + 0.5) | floor))% used"
    + (if $a.red then ", resets \($a.r | at(0))." else "." end)
    + (if $a.red or $a.yellow then c("0") else "" end)
  elif $a.capped then c("38;5;196") + "\($name) used up, resets \($a.r | at($a.len))." + c("0")
  elif $a.red then
    c("38;5;196") + "\($name) \($a.u | pct) used, \($a.pace | x) a sustainable pace: runs out about "
    + "\($a.eta | at($a.len)), resets \($a.r | at($a.len))." + c("0")
  elif $a.yellow then c("38;5;220") + "\($name) \($a.u | pct) used, \($a.pace | x) a sustainable pace." + c("0")
  else "\($name) \($a.u | pct) used." end;

(if type == "object" then . else {} end) as $p
| (($p.rate_limits | objects) // {}) as $rl
| win($rl.five_hour; 18000) as $w5
| win($rl.seven_day; 604800) as $w7
| win($rl.spend_limit; 0) as $wsp
| if $w5 == null and $w7 == null and $wsp == null then "", "", "" else
    rows as $rows
    | analyze($w5; 1; 2; $rows) as $a5
    | analyze($w7; 3; 4; $rows) as $a7
    | analyze($wsp; 0; 0; $rows) as $asp
    | ([$a5, $a7, $asp] | map(select(. != null))) as $all
    | ($all | any(.red)) as $red
    | ($all | any(.yellow)) as $yellow

    # Record this reading when it raises the known maximum for its window.
    | newer($w5; 1; 2; $rows) as $n5
    | newer($w7; 3; 4; $rows) as $n7
    | (if $n5 or $n7 then
         [$now, (if $n5 then $w5.u else "-" end), (if $n5 then $w5.r else "-" end),
                (if $n7 then $w7.u else "-" end), (if $n7 then $w7.r else "-" end)]
         | map(tostring) | join("\t")
       else "" end) as $append

    # The last transcript scan, if recent enough to trust.
    | ($fleet | split("\n")) as $fl
    | (($fl[0] // "") | split("\t")) as $h
    | (if ($h | length) >= 2 and $h[0] == "#" then ($h[1] | tonumber? // null) else null end) as $at
    | (if $at != null and ($now - $at) <= $max_age then
         [ $fl[1:][] | split("\t") | select(length >= 3)
           | {sid: .[0], cost: (.[1] | tonumber? // 0), label: (.[2:] | join(" ") | clean)}
           | select(.cost > 0) ] | sort_by(-.cost)
       else [] end) as $fr
    | ($fr | map(.cost) | add // 0) as $total
    | (if ($top == "always" or ($top == "warn" and $red)) and ($fr | length) >= 2 and $total > 0 then
         $fr[0] as $t
         | {label: (if $t.sid == (($p.session_id | strings) // "") then "this session"
                    elif $t.label == "" then "session " + $t.sid[0:8]
                    else $t.label | trunc(28) end),
            share: (($t.cost / $total * 100 + 0.5) | floor)}
       else null end) as $topinfo
    | (if ($top == "always" or ($top == "warn" and ($red or $yellow)))
          and ($at == null or ($now - $at) >= $every) then "refresh" else "" end) as $refresh

    | (if $style == "long" then
         ([long($a5; "5-hour limit"), long($a7; "Weekly limit"), long($asp; "Spend limit")] | join(" "))
         + (if $topinfo == null then ""
            else " Top spender: \($topinfo.label) (\($topinfo.share)% of recent spend)." end)
       else
         ([short($a5; "5h"), short($a7; "7d"), short($asp; "spend")] | join(" · "))
         + (if $topinfo == null then ""
            else " · " + c("2") + "top:" + c("0") + " \($topinfo.label) \($topinfo.share)%" end)
       end) as $text
    | $append, $refresh, $text
  end'

input=$(cat)
samples_tail=""
[ -f "$samples" ] && samples_tail=$(tail -n 400 "$samples" 2>/dev/null)
fleet_text=""
if [ "$top_mode" != never ]; then
    [ "${USAGE_FORECAST_SYNC:-}" = 1 ] && ( refresh_fleet )
    [ -f "$fleet" ] && fleet_text=$(head -c 65536 "$fleet" 2>/dev/null)
fi
color=1
[ -n "${NO_COLOR:-}" ] && color=""

append=""; refresh=""; text=""
{
    IFS= read -r append
    IFS= read -r refresh
    IFS= read -r text
} < <(printf '%s' "$input" | jq -r --argjson now "$now" --arg samples "$samples_tail" \
        --arg fleet "$fleet_text" --arg style "$style" --arg top "$top_mode" --arg color "$color" \
        --argjson every "$FLEET_EVERY" --argjson max_age "$FLEET_MAX_AGE" "$_render" 2>/dev/null)

if [ -n "$append" ] && mkdir -p "$cache_dir" 2>/dev/null; then
    printf '%s\n' "$append" >>"$samples" 2>/dev/null
    # Keep the file small; a thousand readings is days of history.
    if [ -n "$(find "$samples" -size +128k 2>/dev/null)" ]; then
        tail -n 1000 "$samples" >"$samples.$$" 2>/dev/null && mv "$samples.$$" "$samples" 2>/dev/null
        rm -f "$samples.$$"
    fi
fi

if [ "$refresh" = refresh ] && [ "${USAGE_FORECAST_SYNC:-}" != 1 ] && [ -d "$projects" ]; then
    # Detached, so the status line never waits for it and a cancelled render
    # does not take the scan down with it.
    if command -v setsid >/dev/null 2>&1; then
        setsid bash "${BASH_SOURCE[0]}" --refresh-fleet </dev/null >/dev/null 2>&1 &
    else
        nohup bash "${BASH_SOURCE[0]}" --refresh-fleet </dev/null >/dev/null 2>&1 &
    fi
fi

[ -n "$text" ] && printf '%s\n' "$text"
exit 0
