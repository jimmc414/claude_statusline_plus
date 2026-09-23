#!/bin/bash
# usage_forecast.sh - Claude Code status line segment: will your usage limits
# last until they reset, and which session is burning them?
#
# Reads the status line JSON payload on stdin and prints a single segment:
#
#     5h 12% (resets in 3h00m) · 7d 41% · Fable 78%        on track
#     5h 48% 1.3× (resets in 2h40m)                          (yellow) burning faster than the window lasts
#     5h 53% 3.9× cap 15:36 (resets in 4h20m)                (red) at this pace the limit runs out first
#     ... · top: refactor auth 57%                           the session doing most of the burning
#
# A limit never disappears at 0%. After a reset, Claude Code drops the window
# until a response brings the next one; the segment then shows the newest
# window another session recorded, or the account fetch, or 0%.
#
# The 5-hour limit always shows a countdown to its reset, and its percentage is
# green, then yellow from 75% and red from 90%, the level where Claude Code
# itself warns. The pace and cap time keep their own colors.
#
# Prints nothing when there is no limit to show: API-key sessions, and
# subscription sessions before their first response.
#
# The 5-hour and weekly limits come from the status line payload. Limits scoped
# to one model, such as the weekly Fable limit, are not in the payload; they come
# from the account usage endpoint behind /usage, fetched in the background every
# 5 minutes with your Claude Code login. The login is only read: the token is
# never refreshed, stored, or put on a command line.
#
# Pace is the burn rate as a multiple of the rate that uses exactly 100% over a
# full window: 1.0× lasts the window, 4.0× empties it in a quarter of it. The rate
# comes from readings recorded whenever a limit rises. Every session on the
# machine shares them, so they follow the whole account, not one session. It is
# measured over the last 30 minutes (5-hour window) or 6 hours (weekly); until
# that much history exists, it is the average since the window opened.
#
# "top:" names the session with the largest share of recent token spend, read from
# the transcripts under ~/.claude/projects and weighted by list price. It shows
# while a limit is on course to run out and more than one session is spending.
# When only the Fable limit is, "top Fable:" ranks sessions by Fable spend alone.
# The scan runs in the background and is cached, so the status line never waits.
#
# Environment:
#   USAGE_FORECAST_STYLE          short (default) | long (full sentences)
#   USAGE_FORECAST_COUNTDOWN      1 (default) | 0 = no countdown to the 5-hour reset
#   USAGE_FORECAST_LEVELS         where the 5-hour percentage turns yellow,red (default 75,90)
#   USAGE_FORECAST_TOP            warn (default) | always | never
#   USAGE_FORECAST_WINDOW         seconds of spend the "top" share covers (default 1800)
#   USAGE_FORECAST_ACCOUNT        1 (default) | 0 = never read the login or call the usage endpoint
#   USAGE_FORECAST_ACCOUNT_EVERY  seconds between usage-endpoint fetches (default 300)
#   USAGE_FORECAST_KEYCHAIN       1 = on macOS, read the login from the Keychain (may ask once)
#   USAGE_FORECAST_CACHE_DIR      where readings and fetched data live
#                                 (default ${XDG_CACHE_HOME:-~/.cache}/claude-statusline-plus)
#   USAGE_FORECAST_PROJECTS       transcripts to scan (default ~/.claude/projects,
#                                 or $CLAUDE_CONFIG_DIR/projects)
#   USAGE_FORECAST_TAIL_BYTES     bytes read from the end of a large transcript (default 16 MiB)
#   USAGE_FORECAST_ACCOUNT_URL    usage endpoint (for tests)
#   USAGE_FORECAST_NOW            override current epoch seconds (for tests)
#   USAGE_FORECAST_SYNC           1 = fetch and scan inline, not in the background (for tests)
#   NO_COLOR                      disable ANSI colors

command -v jq >/dev/null 2>&1 || { cat >/dev/null; exit 0; }

style="${USAGE_FORECAST_STYLE:-short}"
top_mode="${USAGE_FORECAST_TOP:-warn}"
window="${USAGE_FORECAST_WINDOW:-1800}"
account_on="${USAGE_FORECAST_ACCOUNT:-1}"
account_every="${USAGE_FORECAST_ACCOUNT_EVERY:-300}"
account_url="${USAGE_FORECAST_ACCOUNT_URL:-https://api.anthropic.com/api/oauth/usage}"
cache_dir="${USAGE_FORECAST_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/claude-statusline-plus}"
config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
projects="${USAGE_FORECAST_PROJECTS:-$config_dir/projects}"
tail_bytes="${USAGE_FORECAST_TAIL_BYTES:-16777216}"
now="${USAGE_FORECAST_NOW:-}"
# Digit caps keep every value inside bash's 64-bit arithmetic.
[[ "$window" =~ ^[0-9]{1,7}$ ]] && [ "$window" -ge 60 ] || window=1800
[[ "$account_every" =~ ^[0-9]{1,7}$ ]] && [ "$account_every" -ge 60 ] || account_every=300
[[ "$tail_bytes" =~ ^[0-9]{1,11}$ ]] && [ "$tail_bytes" -ge 1024 ] || tail_bytes=16777216
[[ "$now" =~ ^[0-9]{1,12}$ ]] || now=$(date +%s)
case "$top_mode" in warn|always|never) ;; *) top_mode=warn ;; esac
case "$style" in short|long) ;; *) style=short ;; esac
[ "$account_on" = 0 ] || account_on=1
countdown="${USAGE_FORECAST_COUNTDOWN:-1}"
[ "$countdown" = 0 ] || countdown=1
levels="${USAGE_FORECAST_LEVELS:-75,90}"
if [[ "$levels" =~ ^([0-9]{1,3}),([0-9]{1,3})$ ]] && [ "${BASH_REMATCH[1]}" -ge 1 ] \
    && [ "${BASH_REMATCH[1]}" -lt "${BASH_REMATCH[2]}" ] && [ "${BASH_REMATCH[2]}" -le 100 ]; then
    yellow_at=${BASH_REMATCH[1]}; red_at=${BASH_REMATCH[2]}
else
    yellow_at=75; red_at=90
fi
projects="${projects%/}"
samples="$cache_dir/usage_samples.tsv"      # 5-hour and weekly readings
fleet="$cache_dir/fleet.tsv"                # last transcript scan
account="$cache_dir/account.tsv"            # model-scoped limits from the last fetch
scoped_samples="$cache_dir/scoped_samples.tsv"
account_stamp="$cache_dir/account.attempt"  # when the endpoint was last tried
FLEET_EVERY=60        # rescan transcripts at most this often (seconds)
FLEET_MAX_AGE=300     # never show a scan older than this
ACCOUNT_MAX_AGE=1200  # never show a fetched limit older than this

# =============================================================================
# Transcript scan: who is spending. Runs in the background (or inline for tests)
# and writes $fleet: a "#<TAB>computed_at<TAB>window" header, then one line per
# session, "session_id<TAB>dollars<TAB>Fable dollars<TAB>name", at list price,
# biggest spender first.
# =============================================================================

# One transcript line in, at most one TSV line out:
#   file U message_id dollars F|-   an API call inside the window (F: a Fable model)
#   file N name                     the session's name (--name or /rename)
#   file T title                    the session's AI-generated title
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
      then [$file, "U", ((.message.id // .requestId // .uuid // "") | tostring), (dollars | tostring),
            (if (.message.model | strings // "") | test("fable|mythos") then "F" else "-" end)] | @tsv
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
    if ($5 == "F") fable[s] += $4
    next
}
($2 == "N" || $2 == "T") && NF >= 3 {
    s = sid_of($1); if (s == "" || !ismain[$1]) next
    if ($2 == "N") name[s] = $3; else title[s] = $3
}
END {
    for (s in cost) if (cost[s] > 0)
        print s, proj[s], cost[s], fable[s] + 0, ((s in name) ? name[s] : ((s in title) ? title[s] : "-"))
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

# take_lock <dir>: an atomic mkdir lock; one left behind by a dead process is
# taken over after 2 minutes.
take_lock() {
    mkdir "$1" 2>/dev/null && return 0
    [ -n "$(find "$1" -maxdepth 0 -mmin +2 2>/dev/null)" ] || return 1
    rm -rf "$1" && mkdir "$1" 2>/dev/null
}

refresh_fleet() {
    [ -d "$projects" ] || return 0
    mkdir -p "$cache_dir" 2>/dev/null || return 0
    _lock="$cache_dir/fleet.lock"
    take_lock "$_lock" || return 0
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
          | while IFS=$'\t' read -r sid proj cost fable label; do
                [ "$label" = "-" ] && label=$(title_of "$projects/$proj/$sid.jsonl" "$sid")
                printf '%s\t%s\t%s\t%s\n' "$sid" "$cost" "$fable" "$label"
            done \
          | sort -t "$(printf '\t')" -k2,2gr
    } >"$tmp" 2>/dev/null && mv "$tmp" "$fleet" 2>/dev/null
    rm -f "$tmp"
    find "$cache_dir/titles" -type f -mmin +1440 -exec rm -f {} + 2>/dev/null
    rm -rf "$_lock"
    trap - EXIT
}

# =============================================================================
# Account fetch: model-scoped limits, which the status line payload does not
# carry. Writes $account: a "#<TAB>fetched_at" header, then one line per scoped
# limit, "name<TAB>percent<TAB>resets_at<TAB>window seconds"; and appends a
# reading to $scoped_samples whenever a scoped limit rises.
# =============================================================================

# token_of: the Claude Code login's access token, or nothing. Never refreshed:
# refreshing could invalidate the login Claude Code itself holds. An expired
# token is skipped until Claude Code renews it.
token_of() {
    if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        printf '%s' "$CLAUDE_CODE_OAUTH_TOKEN"; return 0
    fi
    local creds=""
    if [ -f "$config_dir/.credentials.json" ]; then
        creds=$(cat "$config_dir/.credentials.json" 2>/dev/null)
    elif [ "${USAGE_FORECAST_KEYCHAIN:-}" = 1 ] && command -v security >/dev/null 2>&1; then
        creds=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)
    fi
    [ -n "$creds" ] || return 0
    printf '%s' "$creds" | jq -r --argjson now_ms "$(( now * 1000 ))" '
        .claudeAiOauth | objects | select((.accessToken | type) == "string" and .accessToken != "")
        | select(.expiresAt | if type == "number" then . > $now_ms + 60000 else true end)
        | .accessToken' 2>/dev/null
}

# The endpoint lists every limit under .limits; the scoped ones name a model (or
# a surface) in .scope. Reset times arrive as ISO strings with microseconds that
# jitter between fetches, so they are rounded to the minute; a limit with no
# window running reports no reset time, recorded as 0. The plain 5-hour and
# weekly figures are kept too, as @5h and @7d, for a status line whose payload
# has dropped them.
_account='
def epoch:
  capture("^(?<b>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\\.[0-9]+)?(?<z>Z|[+-][0-9]{2}:[0-9]{2})$") as $m
  | (($m.b + "Z") | fromdateiso8601)
    - (if $m.z == "Z" then 0
       else (($m.z[1:3] | tonumber) * 3600 + ($m.z[4:6] | tonumber) * 60)
            * (if $m.z[0:1] == "-" then -1 else 1 end) end);
def scopename: [.scope | objects | (.model, .surface) | objects | .display_name | strings
                | gsub("[[:cntrl:]]"; "") | select(length > 0)] | first;
def resetof: ((.resets_at | strings | try epoch catch null) // null)
  | if . == null then 0 else ((. + 30) / 60 | floor) * 60 end;
[ .limits[]? | objects
  | scopename as $name | select($name != null)
  | (.percent | numbers) as $u
  | {name: $name[0:16], u: $u, r: resetof, len: (if .group == "session" then 18000 else 604800 end)} ] as $limits
| [ (.five_hour | objects | {name: "@5h", u: (.utilization | numbers), r: resetof, len: 18000}),
    (.seven_day | objects | {name: "@7d", u: (.utilization | numbers), r: resetof, len: 604800}) ] as $plain
| [ $samples | split("\n")[] | split("\t") | select(length == 4)
    | [(.[0] | tonumber? // null), .[1], (.[2] | tonumber? // null), (.[3] | tonumber? // null)] ] as $seen
| ($limits[], $plain[] | "W\t\(.name)\t\(.u)\t\(.r)\t\(.len)"),
  ($limits[] | select(.r > 0) | . as $l
   | ([$seen[] | select(.[1] == $l.name and .[3] == $l.r) | .[2] | numbers] | max) as $max
   | select($max == null or $l.u > $max)
   | "S\t\($now)\t\($l.name)\t\($l.u)\t\($l.r)")'

refresh_account() {
    mkdir -p "$cache_dir" 2>/dev/null || return 0
    printf '%s\n' "$now" >"$account_stamp" 2>/dev/null   # no retry storm when a fetch fails
    command -v curl >/dev/null 2>&1 || return 0
    _alock="$cache_dir/account.lock"
    take_lock "$_alock" || return 0
    trap 'rm -rf "$_alock"' EXIT
    local token reply code body parsed tmp="$account.$$"
    token=$(token_of)
    if [ -n "$token" ]; then
        # The token reaches curl on stdin, never on a command line.
        reply=$(printf 'Authorization: Bearer %s\n' "$token" \
            | curl -sS --max-time 10 -H @- -H 'anthropic-beta: oauth-2025-04-20' -H 'Accept: application/json' \
                   -w '\n%{http_code}' "$account_url" 2>/dev/null)
        token=""
        code=${reply##*$'\n'}
        body=${reply%$'\n'*}
        if [ "$code" = 200 ] && parsed=$(printf '%s' "$body" | jq -r --argjson now "$now" \
                --arg samples "$(tail -n 400 "$scoped_samples" 2>/dev/null)" "$_account" 2>/dev/null); then
            {
                printf '#\t%s\n' "$now"
                printf '%s\n' "$parsed" | awk -F'\t' -v OFS='\t' '$1 == "W" { print $2, $3, $4, $5 }'
            } >"$tmp" 2>/dev/null && mv "$tmp" "$account" 2>/dev/null
            rm -f "$tmp"
            printf '%s\n' "$parsed" | awk -F'\t' -v OFS='\t' '$1 == "S" { print $2, $3, $4, $5 }' \
                >>"$scoped_samples" 2>/dev/null
            if [ -n "$(find "$scoped_samples" -size +64k 2>/dev/null)" ]; then
                tail -n 500 "$scoped_samples" >"$scoped_samples.$$" 2>/dev/null \
                    && mv "$scoped_samples.$$" "$scoped_samples" 2>/dev/null
                rm -f "$scoped_samples.$$"
            fi
        fi
    fi
    rm -rf "$_alock"
    trap - EXIT
}

case "${1:-}" in
    --refresh-fleet)   refresh_fleet;   exit 0 ;;
    --refresh-account) refresh_account; exit 0 ;;
esac

# =============================================================================
# Render: one jq call over the payload, the recent readings, the last scan and
# the last account fetch. Prints three lines: a reading to record (or nothing),
# the background jobs that are due ("fleet", "account"), and the segment.
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

# A window, or null when it is missing, malformed, already past its reset, or
# claims to reset further out than a window lasts.
def win($o; $len):
  if ($o | type) != "object" then null else
    ($o.used_percentage | num) as $u | ($o.resets_at | num) as $r
    | if $u == null or $r == null or $u < 0 or $u > 1000 then null
      elif $r <= $now then null
      elif $len > 0 and $r > $now + $len + 3600 then null
      else {u: $u, r: ($r | floor), len: $len} end
  end;

# A limit from the account fetch. With no reset time, no window is running: show
# its percentage as it is. With a reset that has passed since the fetch, the
# limit has restarted at 0.
def fetched($s):
  if ($s.u | num) == null or $s.u < 0 or $s.u > 1000 then null
  elif $s.r == 0 then {u: $s.u, r: 0, len: $s.len}
  elif $s.r <= $now then {u: 0, r: 0, len: $s.len}
  elif $s.r > $now + $s.len + 3600 then null
  else {u: $s.u, r: $s.r, len: $s.len} end;

# A 5-hour or weekly limit missing from the payload: the newest window still
# running that any session recorded, else the account fetch, else 0%. Only for
# a limit known to exist, from a reading or a fetch: nothing is invented.
def gap($rows; $ui; $ri; $len; $fetch):
  [$rows[] | select((.[$ri] | numbers) != null and (.[$ui] | numbers) != null)] as $seen
  | [$seen[] | select(.[$ri] > $now and .[$ri] <= $now + $len + 3600)] as $cur
  | if ($cur | length) > 0 then
      ($cur | map(.[$ri]) | max) as $r
      | {u: ([$cur[] | select(.[$ri] == $r) | .[$ui]] | max), r: $r, len: $len}
    elif $fetch != null then (fetched($fetch) // {u: 0, r: 0, len: $len})
    elif ($seen | length) > 0 then {u: 0, r: 0, len: $len}
    else null end;

# 5-hour and weekly readings: epoch, 5h used, 5h reset, 7d used, 7d reset.
def rows: [ $samples | split("\n")[] | split("\t") | select(length == 5)
            | map(if . == "-" or . == "" then null else (tonumber? // null) end)
            | select(.[0] != null) ];
# Scoped readings: epoch, name, used, reset.
def srows: [ $scoped | split("\n")[] | split("\t") | select(length == 4)
             | [(.[0] | tonumber? // null), .[1], (.[2] | tonumber? // null), (.[3] | tonumber? // null)]
             | select(.[0] != null and .[2] != null and .[3] != null) ];

# True when this payload raises the highest reading recorded for its window.
# Usage never falls inside a window, so a lower value is a stale session.
def newer($w; $ui; $ri; $rows):
  $w != null and (([$rows[] | select(.[$ri] == $w.r) | .[$ui] | numbers] | max) as $m
                  | ($m == null or $w.u > $m));

# [epoch, used] pairs recorded for a window.
def pairs($rows; $ui; $ri; $w):
  if $w == null then [] else [$rows[] | select(.[$ri] == $w.r and (.[$ui] | numbers) != null) | [.[0], .[$ui]]] end;

def analyze($w; $ws):
  if $w == null then null
  elif $w.len == 0 then
    {u: $w.u, r: $w.r, len: 0, pace: null, eta: null, capped: ($w.u >= 100),
     red: ($w.u >= 100), yellow: ($w.u >= 80)}
  elif $w.r == 0 then
    # No window running (a reset has passed and nothing is known since): no pace,
    # no forecast, no countdown.
    {u: $w.u, r: 0, len: $w.len, pace: null, eta: null, capped: ($w.u >= 100),
     red: ($w.u >= 100), yellow: false}
  else
    ([$w.u, ([$ws[] | .[1]] | max // 0)] | max) as $u
    | $w.len as $L
    | (if $L == 18000 then 1800 else 21600 end) as $look
    | (if $L == 18000 then 600 else 3600 end) as $span
    # Usage at the start of the look-back: the last reading before it (flat until
    # the next one), else the oldest reading inside it.
    | ([$ws[] | select(.[0] < $now - $look)] | max_by(.[0])) as $prev
    | (if $prev != null then [$now - $look, $prev[1]]
       else ([$ws[] | select(.[0] <= $now)] | min_by(.[0])) end) as $ref
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
    | (if $a.r > 0 then c("2") + " (resets " + ($a.r | at($a.len)) + ")" + c("0") else "" end) as $resets
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
  elif $a.capped then
    c("38;5;196") + "\($name) used up" + (if $a.r > 0 then ", resets \($a.r | at($a.len))" else "" end) + "." + c("0")
  elif $a.red then
    c("38;5;196") + "\($name) \($a.u | pct) used, \($a.pace | x) a sustainable pace: runs out about "
    + "\($a.eta | at($a.len)), resets \($a.r | at($a.len))." + c("0")
  elif $a.yellow then c("38;5;220") + "\($name) \($a.u | pct) used, \($a.pace | x) a sustainable pace." + c("0")
  else "\($name) \($a.u | pct) used." end;

# The 5-hour limit: a countdown to its reset, and a percentage colored by how
# close it is to the limit. The pace and cap time keep their forecast colors.
def cd: $countdown == "1";
def dur: floor as $s
  | if $s < 60 then "<1m"
    elif $s < 3600 then "\($s / 60 | floor)m"
    else "\($s / 3600 | floor)h" + ((($s % 3600) / 60 | floor | tostring) | if length < 2 then "0" + . else . end) + "m" end;
def level($u): if $u >= $red_at then c("38;5;196") elif $u >= $yellow_at then c("38;5;220") else c("38;5;40") end;

def short5($a):
  if $a == null then empty else
    (c("2") + "5h" + c("0") + " ") as $head
    | (if $a.r == 0 then ""
       elif cd then " (resets in " + (($a.r - $now) | dur) + ")"
       else c("2") + " (resets " + ($a.r | at($a.len)) + ")" + c("0") end) as $resets
    | if $a.capped then $head + c("38;5;196") + "100% capped" + c("0") + $resets
      else
        $head + level($a.u) + ($a.u | pct) + c("0")
        + (if $a.red then " " + c("38;5;196") + ($a.pace | x) + " cap " + ($a.eta | at($a.len)) + c("0")
           elif $a.yellow then " " + c("38;5;220") + ($a.pace | x) + c("0")
           else "" end)
        + (if cd or $a.red then $resets else "" end)
      end
  end;

def long5($a):
  if $a == null then empty else
    (if $a.r == 0 then ""
     elif cd then ", resets in " + (($a.r - $now) | dur)
     elif $a.red or $a.capped then ", resets " + ($a.r | at($a.len))
     else "" end) as $rs
    | (if $a.red or $a.u >= $red_at then c("38;5;196")
       elif $a.yellow or $a.u >= $yellow_at then c("38;5;220") else c("38;5;40") end)
    + (if $a.capped then "5-hour limit used up\($rs)."
       elif $a.red then "5-hour limit \($a.u | pct) used, \($a.pace | x) a sustainable pace: runs out about "
                        + "\($a.eta | at($a.len))\($rs)."
       elif $a.yellow then "5-hour limit \($a.u | pct) used, \($a.pace | x) a sustainable pace\($rs)."
       else "5-hour limit \($a.u | pct) used\($rs)." end)
    + c("0")
  end;

(if type == "object" then . else {} end) as $p
| (($p.rate_limits | objects) // {}) as $rl
| win($rl.five_hour; 18000) as $p5
| win($rl.seven_day; 604800) as $p7
| win($rl.spend_limit; 0) as $wsp
| ($p5 != null or $p7 != null) as $sub   # a Claude subscription session

# The last account fetch, if recent enough to trust. Its limits belong to the
# Claude login, so they only count in a subscription session: an API-key or
# gateway session does not draw on them.
| ($account | split("\n")) as $al
| (($al[0] // "") | split("\t")) as $ah
| (if ($ah | length) >= 2 and $ah[0] == "#" then ($ah[1] | tonumber? // null) else null end) as $aat
| (if $sub and $aat != null and ($now - $aat) <= $acct_max_age then
     [ $al[1:][] | split("\t") | select(length == 4)
       | {name: (.[0] | clean | trunc(16)), u: (.[1] | tonumber? // null),
          r: (.[2] | tonumber? // null), len: (.[3] | tonumber? // null)}
       | select(.name != "" and .r != null and (.len == 18000 or .len == 604800)) ]
   else [] end) as $acct
| [ $acct[] | select(.name | startswith("@") | not) | . as $s | fetched($s) | select(. != null)
    | . + {name: $s.name} ] as $sw

# Claude Code drops a window from the payload once its reset passes, and sends
# the next one only after a response carries it. Until then the limit still
# shows: from a newer window another session recorded, else from the account
# fetch, else at 0%.
| rows as $rows
| (if $p5 != null then $p5
   elif $sub then gap($rows; 1; 2; 18000; ([$acct[] | select(.name == "@5h")] | first))
   else null end) as $w5
| (if $p7 != null then $p7
   elif $sub then gap($rows; 3; 4; 604800; ([$acct[] | select(.name == "@7d")] | first))
   else null end) as $w7

| if $w5 == null and $w7 == null and $wsp == null then "", "", "" else
    srows as $srows
    | analyze($w5; pairs($rows; 1; 2; $w5)) as $a5
    | analyze($w7; pairs($rows; 3; 4; $w7)) as $a7
    | analyze($wsp; []) as $asp
    | [ $sw[] | . as $w
        | analyze($w; [$srows[] | select(.[1] == $w.name and .[3] == $w.r) | [.[0], .[2]]])
        | . + {name: $w.name} ] as $as
    | ([$a5, $a7, $asp] | map(select(. != null))) as $main
    | ($main | any(.red)) as $red_main
    | (($main + $as) | any(.red)) as $red
    | (($main + $as) | any(.yellow)) as $yellow
    | ($as | map(select(.name | ascii_downcase | test("fable"))) | any(.red)) as $red_fable

    # Record this reading when it raises the known maximum for its window.
    | newer($p5; 1; 2; $rows) as $n5
    | newer($p7; 3; 4; $rows) as $n7
    | (if $n5 or $n7 then
         [$now, (if $n5 then $p5.u else "-" end), (if $n5 then $p5.r else "-" end),
                (if $n7 then $p7.u else "-" end), (if $n7 then $p7.r else "-" end)]
         | map(tostring) | join("\t")
       else "" end) as $append

    # The last transcript scan, if recent enough to trust. When only the Fable
    # limit is running out, sessions are ranked by what they spent on Fable.
    | ($fleet | split("\n")) as $fl
    | (($fl[0] // "") | split("\t")) as $h
    | (if ($h | length) >= 2 and $h[0] == "#" then ($h[1] | tonumber? // null) else null end) as $at
    | (($red_main | not) and $red_fable) as $by_fable
    | (if $at != null and ($now - $at) <= $max_age then
         [ $fl[1:][] | split("\t") | select(length >= 4)
           | {sid: .[0], v: (if $by_fable then .[2] else .[1] end | tonumber? // 0),
              label: (.[3:] | join(" ") | clean)}
           | select(.v > 0) ] | sort_by(-.v)
       else [] end) as $fr
    | ($fr | map(.v) | add // 0) as $total
    | (($p.session_id | strings) // "") as $me
    | (if ($top == "always" or ($top == "warn" and $red)) and ($fr | length) >= 1 and $total > 0
          and (($fr | length) >= 2 or $fr[0].sid != $me) then
         $fr[0] as $t
         | {label: (if $t.sid == $me then "this session"
                    elif $t.label == "" then "session " + $t.sid[0:8]
                    else $t.label | trunc(28) end),
            share: (($t.v / $total * 100 + 0.5) | floor)}
       else null end) as $topinfo

    | ([ (if ($top == "always" or ($top == "warn" and ($red or $yellow)))
             and ($at == null or ($now - $at) >= $every) then "fleet" else empty end),
         (if $acct_on == "1" and $sub and ($now - $acct_attempt) >= $acct_every
          then "account" else empty end) ] | join(" ")) as $jobs

    | (if $style == "long" then
         ([long5($a5), long($a7; "Weekly limit"),
           ($as[] | long(.; "\(.name) \(if .len == 18000 then "5-hour" else "weekly" end) limit")),
           long($asp; "Spend limit")] | join(" "))
         + (if $topinfo == null then ""
            else " Top \(if $by_fable then "Fable spender" else "spender" end): \($topinfo.label)"
                 + " (\($topinfo.share)% of recent \(if $by_fable then "Fable " else "" end)spending)." end)
       else
         ([short5($a5), short($a7; "7d"), ($as[] | short(.; .name)), short($asp; "spend")] | join(" · "))
         + (if $topinfo == null then ""
            else " · " + c("2") + (if $by_fable then "top Fable:" else "top:" end) + c("0")
                 + " \($topinfo.label) \($topinfo.share)%" end)
       end) as $text
    | $append, $jobs, $text
  end'

input=$(cat)
sync="${USAGE_FORECAST_SYNC:-}"
color=1
[ -n "${NO_COLOR:-}" ] && color=""

load_state() {
    samples_tail=""; fleet_text=""; account_text=""; scoped_tail=""; account_attempt=0
    [ -f "$samples" ] && samples_tail=$(tail -n 400 "$samples" 2>/dev/null)
    [ "$top_mode" != never ] && [ -f "$fleet" ] && fleet_text=$(head -c 65536 "$fleet" 2>/dev/null)
    if [ "$account_on" = 1 ]; then
        [ -f "$account" ] && account_text=$(head -c 16384 "$account" 2>/dev/null)
        [ -f "$scoped_samples" ] && scoped_tail=$(tail -n 400 "$scoped_samples" 2>/dev/null)
        [ -f "$account_stamp" ] && account_attempt=$(head -n 1 "$account_stamp" 2>/dev/null)
        [[ "$account_attempt" =~ ^[0-9]{1,12}$ ]] || account_attempt=0
    fi
}

render() {
    append=""; jobs=""; text=""
    {
        IFS= read -r append
        IFS= read -r jobs
        IFS= read -r text
    } < <(printf '%s' "$input" | jq -r --argjson now "$now" --arg samples "$samples_tail" \
            --arg scoped "$scoped_tail" --arg fleet "$fleet_text" --arg account "$account_text" \
            --arg style "$style" --arg top "$top_mode" --arg color "$color" --arg acct_on "$account_on" \
            --arg countdown "$countdown" --argjson yellow_at "$yellow_at" --argjson red_at "$red_at" \
            --argjson acct_attempt "$account_attempt" --argjson acct_every "$account_every" \
            --argjson acct_max_age "$ACCOUNT_MAX_AGE" --argjson every "$FLEET_EVERY" \
            --argjson max_age "$FLEET_MAX_AGE" "$_render" 2>/dev/null)
}

[ "$sync" = 1 ] && [ "$top_mode" != never ] && ( refresh_fleet )
load_state
render
if [ "$sync" = 1 ] && [[ " $jobs " == *" account "* ]]; then
    ( refresh_account )
    load_state
    render
fi

if [ -n "$append" ] && mkdir -p "$cache_dir" 2>/dev/null; then
    printf '%s\n' "$append" >>"$samples" 2>/dev/null
    # Keep the file small; a thousand readings is days of history.
    if [ -n "$(find "$samples" -size +128k 2>/dev/null)" ]; then
        tail -n 1000 "$samples" >"$samples.$$" 2>/dev/null && mv "$samples.$$" "$samples" 2>/dev/null
        rm -f "$samples.$$"
    fi
fi

# Background jobs run detached, so the status line never waits for them and a
# cancelled render does not take them down with it.
spawn() {
    if command -v setsid >/dev/null 2>&1; then
        setsid bash "${BASH_SOURCE[0]}" "$1" </dev/null >/dev/null 2>&1 &
    else
        nohup bash "${BASH_SOURCE[0]}" "$1" </dev/null >/dev/null 2>&1 &
    fi
}
if [ "$sync" != 1 ]; then
    [[ " $jobs " == *" fleet "* ]] && [ -d "$projects" ] && spawn --refresh-fleet
    [[ " $jobs " == *" account "* ]] && spawn --refresh-account
fi

[ -n "$text" ] && printf '%s\n' "$text"
exit 0
