#!/bin/bash
# cache_warm.sh - Claude Code status line segment: is the prompt cache still warm?
#
# Reads the status line JSON payload on stdin and prints a single segment:
#
#     cache warm 31m          next message is a cheap cache read
#     cache warm 4m           (yellow) expiring soon: send now or accept a cold start
#     cache cold · 47k        expired; next message re-caches ~47k tokens at full price
#     cache off               the API is not reporting any prompt caching
#
# Prints nothing if the session has made no API calls yet.
#
# Two sources:
#   1. .prompt_cache in the payload (Claude Code >= 2.1.251): the CLI's own
#      expires_at / ttl / recache_tokens_if_cold. Used as-is when present. It is
#      stamped when each request is dispatched, which is when the API starts the
#      TTL, and it sees cache touches the transcript doesn't record (forked agents).
#   2. The transcript (.transcript_path): each main-chain assistant entry records
#      an API call's usage, including which TTL tier (5 min or 1 hour) it wrote.
#      A cache hit refreshes the TTL, so expiry = last request start + TTL. This
#      is the fallback for older versions, and it supplies the last model used.
#
# For the countdown to tick while the session is idle, set statusLine.refreshInterval
# in settings.json. Claude Code re-runs the status line at expires_at on its own.
#
# Environment:
#   CACHE_WARM_STYLE        short (default) | long ("Prompt cache warm, about 31 min left.")
#   CACHE_WARM_DEFAULT_TTL  TTL in seconds when no source reveals the tier (default 3600)
#   CACHE_WARM_TAIL         transcript lines to scan (default 100)
#   CACHE_WARM_NOW          override current epoch seconds (for tests)
#   NO_COLOR                disable ANSI colors

input=$(cat)
command -v jq &>/dev/null || exit 0

style="${CACHE_WARM_STYLE:-short}"
default_ttl="${CACHE_WARM_DEFAULT_TTL:-3600}"
tail_lines="${CACHE_WARM_TAIL:-100}"
now="${CACHE_WARM_NOW:-}"
# Digit caps keep every value inside bash's 64-bit arithmetic.
[[ "$default_ttl" =~ ^[0-9]{1,9}$ ]] && [ "$default_ttl" -gt 0 ] || default_ttl=3600
[[ "$tail_lines" =~ ^[0-9]{1,9}$ ]] && [ "$tail_lines" -gt 0 ] || tail_lines=100
[[ "$now" =~ ^[0-9]{1,12}$ ]] || now=$(date +%s)

# --- payload ------------------------------------------------------------------
# Line 1: tab-separated fields; "-" stands in for empty because read collapses
# consecutive tabs. pc_state: none (no prompt_cache object) | timed | cold | off.
# Line 2: the transcript path, kept out of @tsv, which would escape backslashes.
{
    IFS=$'\t' read -r model_id pc_state pc_expiry pc_ttl pc_recache
    IFS= read -r transcript
} < <(printf '%s' "$input" | jq -r '
    (if (.prompt_cache | type) == "object" then .prompt_cache else null end) as $pc
    | ([ ((.model | objects | .id | strings) // "" | if . == "" then "-" else . end),
         (if $pc == null then "none"
          elif ($pc.expires_at | type) == "number" then "timed"
          elif $pc.caching_observed == false then "off"
          else "cold" end),
         (if ($pc.expires_at | type) == "number" then ($pc.expires_at | floor) else 0 end),
         (if $pc.ttl == "5m" then 300 elif $pc.ttl == "1h" then 3600 else 0 end),
         (if ($pc.recache_tokens_if_cold | type) == "number" then ($pc.recache_tokens_if_cold | floor) else 0 end)
       ] | @tsv),
      ((.transcript_path | strings) // "")' 2>/dev/null)
[ -n "$pc_state" ] || exit 0
[ "$model_id" = "-" ] && model_id=""
# Negative, fractional-exponent, or absurdly large numbers are not an expiry.
[[ "$pc_expiry" =~ ^[0-9]{1,12}$ ]] || pc_expiry=0
[[ "$pc_ttl" =~ ^[0-9]{1,9}$ ]] || pc_ttl=0
[[ "$pc_recache" =~ ^[0-9]{1,12}$ ]] || pc_recache=0
[ "$pc_state" = timed ] && [ "$pc_expiry" -eq 0 ] && pc_state=none

# --- transcript ---------------------------------------------------------------
# One pass over the tail. Emits: request_start_epoch, ttl, context_tokens, model,
# and cut=1 when the window begins mid-call (so its trigger may be cut off).
# -R + fromjson? skips a partially written last line instead of failing.
_filter='
def ts: sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601;   # jq 1.7 rejects fractional seconds
def n: (numbers // 0);
def tier: ((.cache_creation | objects) // {}) as $c
  | if   ($c.ephemeral_5m_input_tokens | n) > 0 then 300    # 5m wins if mixed: be conservative
    elif ($c.ephemeral_1h_input_tokens | n) > 0 then 3600
    else null end;
# A real API call that touched the cache. API errors and "<synthetic>" entries
# carry a usage block of zeros and must not count. try: a malformed entry is
# skipped rather than aborting the whole scan.
def is_call: try (.type == "assistant" and (.isApiErrorMessage | not)
  and .message.model != "<synthetic>"
  and ((.message.usage.cache_read_input_tokens | n) + (.message.usage.cache_creation_input_tokens | n)) > 0)
  catch false;

[ inputs | fromjson? | objects
  | select((.type == "assistant" or .type == "user") and (.isSidechain | not) and (.timestamp | type) == "string")
  | (try (.timestamp | ts) catch null) as $t | select($t != null)
  | if is_call
    then { call: true, t: $t, model: .message.model, u: .message.usage,
           id: ((.message.id | strings) // (.uuid | strings) // "t\($t)") }
    else { call: false, t: $t } end ] as $chain
| [ $chain[] | select(.call) ] as $calls
| if ($calls | length) == 0 then empty else
    ($calls | last) as $last
    # One API call is written as several lines (one per content block) sharing a
    # message id, and with extended thinking the first of them can land minutes
    # after the request was sent. The cache is touched when the request starts,
    # so anchor on the user/tool-result entry that triggered it when there is one.
    # min: an out-of-band entry stamped later than the call must not extend it.
    | ([ $chain | to_entries[] | select(.value.call and .value.id == $last.id) | .key ] | first) as $i
    | (if $i > 0 and ($chain[$i - 1].call | not)
       then ([$chain[$i - 1].t, $chain[$i].t] | min) else $chain[$i].t end) as $touch
    | ([ $calls[] | .u | tier | select(. != null) ] | last // 0) as $ttl
    | [ $touch, $ttl,
        (($last.u.input_tokens | n) + ($last.u.cache_creation_input_tokens | n) + ($last.u.cache_read_input_tokens | n)),
        (($last.model | strings) // "" | if . == "" then "-" else . end),
        (if $i == 0 then 1 else 0 end) ] | @tsv
  end'

scan() { jq -nrR "$_filter" 2>/dev/null; }

touch=""; t_ttl=0; ctx=0; last_model=""
if [ -n "$transcript" ] && [ -f "$transcript" ] && [ -r "$transcript" ]; then
    result=$(tail -n "$tail_lines" "$transcript" 2>/dev/null | scan)
    # Nothing found, or the window begins mid-call: look further back. Bounded in
    # bytes, not lines, because single transcript lines can approach a megabyte.
    case "$result" in
        ""|*$'\t'1)
            wider=$(tail -c 8388608 "$transcript" 2>/dev/null | scan)
            [ -n "$wider" ] && result=$wider ;;
    esac
    if [ -n "$result" ]; then
        IFS=$'\t' read -r touch t_ttl ctx last_model _ <<<"$result"
        [[ "$touch" =~ ^[0-9]{1,12}$ ]] || touch=""
        [[ "$t_ttl" =~ ^[0-9]{1,9}$ ]] || t_ttl=0
        [[ "$ctx" =~ ^[0-9]{1,12}$ ]] || ctx=0
        [ "$last_model" = "-" ] && last_model=""
    fi
fi

# --- combine ------------------------------------------------------------------
if   [ "$pc_ttl" -gt 0 ]; then ttl=$pc_ttl
elif [ "$t_ttl" -gt 0 ];  then ttl=$t_ttl
else                           ttl=$default_ttl
fi

expiry=""
if [ "$pc_state" = timed ]; then
    expiry=$pc_expiry
elif [ "$pc_state" = none ]; then
    [ -n "$touch" ] || exit 0
    expiry=$(( touch + ttl ))
fi

# Caches are per-model: after /model the next request misses even if time remains.
# Same model = identical ids, ignoring a "[1m]"-style suffix and a -YYYYMMDD date
# stamp. A prefix is not enough: claude-fable-5 and claude-fable-5-1 are different
# models. Only ordinary claude-* ids are compared, so an unfamiliar id format
# (a provider ARN, an alias) can never read as a permanent model change.
same_model() { [[ "$1" == "$2" || "$1" =~ ^"$2"-[0-9]{8}$ || "$2" =~ ^"$1"-[0-9]{8}$ ]]; }
model_changed=0
a="${model_id%%[*}"; b="${last_model%%[*}"
if [[ "$a" == claude-* && "$b" == claude-* ]]; then
    same_model "$a" "$b" || model_changed=1
fi

why=""; remaining=0
if   [ "$pc_state" = off ];  then state=off
elif [ "$pc_state" = cold ]; then state=cold
elif [ "$model_changed" -eq 1 ]; then state=cold; why="model changed"
else
    remaining=$(( expiry - now ))
    # A sliding TTL can never leave more than one full TTL on the clock.
    [ "$remaining" -gt "$ttl" ] && remaining=$ttl
    # Warn in the last sixth of the TTL (10 min of 1h), but never less than 60s.
    warn=$(( ttl / 6 )); [ "$warn" -lt 60 ] && warn=60
    if   [ "$remaining" -le 0 ];       then state=cold
    elif [ "$remaining" -le "$warn" ]; then state=expiring
    else                                    state=warm
    fi
fi

# --- render -------------------------------------------------------------------
if [ -n "${NO_COLOR:-}" ]; then
    green=""; yellow=""; blue=""; dim=""; reset=""
else
    green=$'\033[38;5;40m'; yellow=$'\033[38;5;220m'; blue=$'\033[38;5;39m'
    dim=$'\033[2m'; reset=$'\033[0m'
fi

# Round down: under-promising is the safe direction for a countdown.
if [ "$remaining" -ge 60 ]; then left="$(( remaining / 60 ))m"; left_long="about $(( remaining / 60 )) min"
else left="<1m"; left_long="under a minute"; fi

# A 5-minute window on a subscription means usage credits have kicked in; say so.
short_ttl=""; long_ttl=""
[ "$ttl" -eq 300 ] && { short_ttl=" (5m ttl)"; long_ttl=" (5 min TTL)"; }

recache=$pc_recache; [ "$recache" -gt 0 ] || recache=$ctx
if   [ "$recache" -ge 1000 ]; then recache_lbl="$(( (recache + 500) / 1000 ))k"
elif [ "$recache" -gt 0 ];    then recache_lbl="$recache"
else                               recache_lbl=""
fi

case "$style:$state" in
    long:warm)      echo "${green}Prompt cache warm, ${left_long} left${long_ttl}.${reset}" ;;
    long:expiring)  echo "${yellow}Prompt cache expiring, ${left_long} left${long_ttl}.${reset}" ;;
    long:cold)      echo "${blue}Prompt cache cold${why:+ ($why)}${recache_lbl:+; next message re-caches ~${recache_lbl} tokens}.${reset}" ;;
    long:off)       echo "${dim}Prompt caching not reported by the API.${reset}" ;;
    *:warm)         echo "${dim}cache${reset} ${green}warm ${left}${short_ttl}${reset}" ;;
    *:expiring)     echo "${dim}cache${reset} ${yellow}warm ${left}${short_ttl}${reset}" ;;
    *:cold)         echo "${dim}cache${reset} ${blue}cold${why:+ ($why)}${reset}${recache_lbl:+${dim} · ${recache_lbl}${reset}}" ;;
    *:off)          echo "${dim}cache off${reset}" ;;
esac
