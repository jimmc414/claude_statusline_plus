# claude_statusline_plus

Claude Code status line segments that show what the CLI knows but does not display: whether your prompt cache is still warm, and whether your usage limits will last until they reset.

```
cache warm 31m · 5h 12% · 7d 41% · Fable 50%
cache warm 31m · 5h 53% 3.9× cap 15:36 (resets 19:20) · 7d 38% · Fable 50% · top: refactor auth 57%
```

| Segment | The question it answers |
|---|---|
| `cache_warm.sh` | Is the prompt cache still warm, and what will a cold start cost? |
| `usage_forecast.sh` | At this pace, will my 5-hour, weekly and Fable limits last until they reset, and which session is burning them? |
| `statusline_plus.sh` | Runs both and joins what they print. The installer uses it when you have no status line of your own. |

Each segment is a standalone script with the same contract: status line JSON on stdin, one line on stdout, nothing at all when there is nothing to report, and always exit 0.

## Problem

**The prompt cache.** Every request Claude Code sends includes the entire conversation so far. Prompt caching makes that affordable: a cached prefix is read at a fraction of the normal input price. But the cache expires after a period of inactivity, and nothing in the terminal tells you when. So you step away from a 300k-token session, come back 65 minutes later, type "ok, continue", and that one short message re-processes all 300k tokens at full price. On a subscription that is a visible bite out of your usage window. The expiry is invisible, and the cost lands on whichever message happens to come next.

**The usage limits.** A Claude subscription meters usage in a rolling 5-hour window and a weekly one. Claude Code hands the status line both percentages, but a percentage alone does not say whether you will make it. 53% is comfortable three hours into a window and a lockout forty minutes in. And the meter is account-wide: with several sessions, subagents, or workflows running, nothing tells you which one is burning it. Fable also has a weekly allowance of its own, which Claude Code never passes to the status line at all.

The information needed to act on both exists. It just is not displayed anywhere in the CLI.

## Approach

**`cache_warm.sh`** renders the cache's remaining lifetime as a countdown. Claude Code (2.1.251 and later) passes every status line script a `prompt_cache` object containing the cache's expiry time, its TTL, and the number of tokens that would be re-cached if it went cold. On older versions, which lack that object, the segment reconstructs the same answer from the session transcript. It also overlays one check the raw expiry does not give you: caches are per-model, so right after `/model` the next request is a full miss even when the clock says there is time left. The segment reports that as cold.

```
cache warm 31m          next message is a cheap cache read
cache warm 4m           (yellow) expiring soon: send now or accept a cold start
cache cold · 140k       expired; next message re-caches ~140k tokens at full price
cache cold (model changed) · 140k
cache warm 3m (5m ttl)  you are on the 5-minute window, not the 1-hour one
cache off               the API is not reporting any prompt caching
```

**`usage_forecast.sh`** turns each limit's percentage into a pace and a forecast. Pace is your burn rate as a multiple of the rate that would use exactly 100% over a full window: `1.0×` lasts the window, `4.0×` empties it in a quarter of it. When the current pace runs the limit out before it resets, the segment turns red and says when. While a limit is heading for that, it also names the session doing most of the spending, read from the transcripts Claude Code keeps on disk. It also shows limits scoped to one model, such as the weekly Fable allowance, which it reads from the same account endpoint as the `/usage` screen.

```
5h 12% · 7d 41% · Fable 50%              on track
5h 20% 1.2×                              (yellow) burning faster than the window lasts, but it will last
Fable 78% 1.3× cap Tue 19:58 (resets Thu 14:20)   (red) the Fable allowance runs out first
5h 53% 3.9× cap 15:36 (resets 19:20)     (red) at this pace the limit runs out at 15:36
5h 100% capped (resets 19:20)            (red) used up until 19:20
... · top: refactor auth 57%             that session did 57% of the recent spending
```

Knowing the state changes what you do:

- **Warm cache, plenty of time:** carry on.
- **Yellow cache:** send your next message now, or accept that the following one will be a cold start.
- **Cold cache:** the full re-cache is coming regardless, so this is the cheapest moment to `/compact` or `/clear` first. Both invalidate the cache anyway; doing them while it is already cold costs nothing extra.
- **Red limit:** pause or stop the top spender, move it to a cheaper model, or hold off on the next parallel wave. The cap time tells you how long you have; the reset time tells you how long a lockout would last.
- **Yellow limit:** you are burning faster than the window lasts, but at this rate it will not run out. Starting something heavy now would change that.

## Installation

Requires `bash` and `jq`. Works on Linux, macOS, and WSL.

```bash
curl -fsSL https://raw.githubusercontent.com/jimmc414/claude_statusline_plus/main/install.sh | bash
```

Or from a clone:

```bash
git clone https://github.com/jimmc414/claude_statusline_plus
cd claude_statusline_plus
./install.sh
```

What the installer does:

- Copies `cache_warm.sh`, `usage_forecast.sh` and `statusline_plus.sh` into your Claude Code config directory (`~/.claude`, or `$CLAUDE_CONFIG_DIR`).
- **If you have no status line,** configures `statusline_plus.sh` as one in `settings.json`, after backing the file up to `settings.json.bak-statusline-plus`. Your other settings are preserved.
- **If your status line is this project's older `cache_warm.sh` setup,** upgrades it to `statusline_plus.sh`, keeping your `refreshInterval`.
- **If you already have a status line of your own, it changes nothing in your settings.** It prints the lines to add to your existing script instead (see [Claude Code Integration](#claude-code-integration)).

### Global or per-project

```bash
./install.sh                    # every session (default): ~/.claude/settings.json
./install.sh --project          # only the project in the current directory
./install.sh --project ~/code/myapp
```

`--project` installs into that project's `.claude/` directory and writes `.claude/settings.local.json`, which is the personal, uncommitted settings file. A project-level status line replaces the status line from any broader scope, so if you already have a custom one globally or in the project's shared `.claude/settings.json`, the installer will not shadow it and prints the manual steps instead.

With the one-liner, pass options after `bash -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/jimmc414/claude_statusline_plus/main/install.sh | bash -s -- --project
```

### Uninstall

```bash
./install.sh --uninstall
./install.sh --uninstall --project ~/code/myapp
```

Removes the three scripts, and removes the `statusLine` entry only if it is one this installer wrote. `usage_forecast.sh` keeps its readings and fetched limits in `~/.cache/claude-statusline-plus`; delete that directory too if you want no trace.

## Usage

Nothing to run. After installing, the segments appear once the session has made its first API call, and each disappears again when it has nothing to report. The usage segment appears only on a Claude subscription: API-key sessions get no `rate_limits` from Claude Code.

To try a segment by hand, pipe it a payload:

```bash
echo '{"prompt_cache":{"caching_observed":true,"ttl":"1h","expires_at":'$(( $(date +%s) + 1860 ))'}}' \
  | bash ~/.claude/cache_warm.sh
# cache warm 31m

now=$(date +%s)   # a throwaway cache keeps this made-up reading out of your real ones
echo '{"rate_limits":{"five_hour":{"used_percentage":52,"resets_at":'$(( now + 15600 ))'}}}' \
  | USAGE_FORECAST_CACHE_DIR=$(mktemp -d) bash ~/.claude/usage_forecast.sh
# 5h 52% 3.9× cap 14:37 (resets 18:20)    (40 minutes into a window; the times will be yours)
```

## How It Works

### The prompt cache

#### What "warm" means

Caching is prefix-based. Each request's prompt is the tool definitions, the system prompt, and the full conversation history. On a hit, the API reads the entire matching prefix from cache and refreshes the expiry of all of it; only the new suffix is written. So the whole conversation stays warm together, the window slides forward with every request, and old turns do not age out while you keep working.

The TTL is measured from when a request is sent, not from when the response finishes. A response that takes four minutes to generate leaves 56 minutes, not 60.

#### Which TTL you get

Claude Code requests a 1-hour TTL for the main conversation on a Claude subscription within its included usage, and 5 minutes otherwise: API keys, Bedrock, Vertex, Foundry, or a subscription that has moved onto usage credits. The segment reads the TTL rather than assuming it, and labels the 5-minute case `(5m ttl)` so a silent drop from the 1-hour window is visible.

#### Sources, in order

1. **`prompt_cache` in the status line payload** (Claude Code 2.1.251+). Used as-is. The CLI stamps it when each request is dispatched, which matches how the API measures the TTL, and it records cache touches that never reach the transcript, such as a forked agent reading the main conversation's cache.
2. **The session transcript** (older versions). Each main-chain assistant entry records an API call's `usage`, including `cache_creation.ephemeral_1h_input_tokens` versus `ephemeral_5m_input_tokens`, which reveals the TTL tier. Expiry is the last request's start time plus the TTL.

Details that matter in the transcript path:

- One API call is written as several lines, one per content block, sharing a message id. With extended thinking the first block can land minutes after the request was sent, so the anchor is the user or tool-result entry that triggered the call, not the response.
- API-error and `<synthetic>` entries carry a zeroed usage block and are ignored.
- Subagent calls live in separate transcript files and use their own 5-minute caches. They do not keep the main conversation warm and are not counted.
- Only the tail of the transcript is read, so cost stays flat as sessions grow. Transcript lines can approach a megabyte each, so the fallback scan is bounded in bytes, not lines.

#### How sharp the expiry is

Measured across roughly 35,000 main-conversation API calls from one machine's transcripts, comparing the idle gap before each call with whether it hit the cache: the longest gap that still hit was 59.84 minutes, and the shortest that missed was 60.46 minutes. Nothing hit past the hour.

Before the hour it is not a guarantee. Gaps of 55 to 60 minutes hit 4 times out of 5, and about 0.4% of calls missed after short gaps for reasons not visible in the transcript. That is why the segment turns yellow for the last sixth of the window rather than pretending the final minutes are safe, and why minutes are rounded down.

### The usage forecast

#### Pace and the forecast

The payload's `rate_limits` object gives each window's used percentage and reset time. A window's length is fixed (5 hours, or 7 days), so the reset time also says when the window opened. The segment needs one more number, the current burn rate, and takes it from two places:

1. **Recent readings.** Whenever a status line render sees a window's percentage rise above the highest value recorded for it, the segment appends a reading to `~/.cache/claude-statusline-plus/usage_samples.tsv`. The rate is the rise over the last 30 minutes for the 5-hour window, or 6 hours for the weekly one.
2. **The window average,** when there are not yet enough readings: the percentage used divided by the time since the window opened. It is not used in a window's first 5% (15 minutes of a 5-hour window), where a single burst would read as an enormous pace.

The meter reports whole percentages, so a pace built on little history is coarse. On the weekly window one point is 1.7 hours at `1.0×`, so a single point in the first hour after installing reads as about `1.6×`. The figure steadies as the look-back fills.

The cap time is when the used percentage reaches 100% at that rate. The segment turns red when that falls before the reset, and yellow when the pace is at least `1.0×` but the reset comes first. A pace from the window average can never be yellow: at `1.0×` or more over the whole window so far, the rest of the window runs out too.

#### Readings are shared across sessions

The meter belongs to the account, so every session on the machine records into the same readings file, and each render uses all of them. Two rules keep that honest:

- **Only a new high is recorded.** Usage never falls inside a window, so a session whose last response is old, and which therefore reports an older, lower percentage, adds nothing.
- **The freshest value wins.** An idle session displays the highest reading any session has seen for the window, not its own stale one.

The file keeps its last thousand readings, a few days of history.

#### Limits scoped to one model

Claude Code's status line payload carries only the 5-hour and weekly limits. The weekly Fable allowance, and any other limit scoped to one model, is listed only by the account endpoint behind the `/usage` screen, `https://api.anthropic.com/api/oauth/usage`, in its `limits` list, where a scoped entry names its model in `scope`. The segment fetches it itself:

- **With your Claude Code login, read-only.** The access token comes from `~/.claude/.credentials.json`, or `CLAUDE_CODE_OAUTH_TOKEN` when that is set. It is never refreshed, because refreshing could sign Claude Code itself out; an expired token is skipped until Claude Code renews it. It is never written anywhere, and it reaches `curl` on stdin, so it never shows in a process list.
- **In the background, at most every 5 minutes,** and only from sessions that show subscription limits. One fetch serves every session on the machine.
- **Failures fade out.** A failed fetch keeps the last result for up to 20 minutes, and after that the scoped limit disappears rather than going stale.
- **Same forecast.** Each scoped limit gets the same pace and cap time, from readings recorded whenever it rises. The endpoint's reset times jitter by fractions of a second between fetches, so they are rounded to the minute.
- **Its own top spender.** When only the Fable limit is running out, `top Fable:` ranks sessions by what they spent on Fable models alone.

`USAGE_FORECAST_ACCOUNT=0` turns all of this off: no login is read and no request is made.

#### Who is spending

While a limit is heading for its cap, the segment scans the transcripts under `~/.claude/projects` that changed in the last 30 minutes, including subagent and workflow transcripts, which count toward their parent session. It totals each session's API calls in that period at list price, and names the largest share. Details:

- Each call counts once. Claude Code writes one line per content block, all carrying the same usage.
- Tokens are priced per model: input, output, cache reads, and cache writes at 1.25× input for the 5-minute tier and 2× for the 1-hour tier. For example, a Fable 5.1 cache read costs $0.25 per million tokens and an Opus 5 one $0.50.
- The name is the one set with `--name` or `/rename` when there is one, else the session's AI-generated title. The session you are in reads `this session`.
- Large transcripts are read from the tail only, and only the lines that matter.
- The scan runs in the background, at most once a minute, with a lock so parallel sessions do not all scan at once. The status line never waits for it, so a name can take one refresh to appear.

#### How well it works

Replayed through this script, three weeks of one account's rate-limit readings (138 readings, 26 five-hour windows) turned red 49 minutes before the only 5-hour lockout in that period. They also turned red in 6 of the 25 windows that never ran out, where the heavy work stopped in time. Read red as "at this pace", not as a prediction. Those readings were sparse; a live status line records far more of them.

Over the same period, the machine's transcripts explained about half of the meter's movement. The rest is usage this machine cannot see, such as claude.ai or another computer, and the difference between list prices and how the limits weigh tokens. That is why the top spender is shown as a share of recent spending, not as percentage points of your limit.

### Staying current while idle

A status line only re-runs on events, so a countdown would freeze exactly when you stop typing. Three things prevent that:

- `statusLine.refreshInterval` re-runs the command every N seconds. The installer sets it to 30.
- Claude Code schedules a re-run for the moment `prompt_cache.expires_at` passes, so the change from warm to cold repaints on its own even without a refresh interval.
- It does the same when a rate-limit window reaches its `resets_at`.

## Commands / API

### install.sh

| Option | Effect |
|---|---|
| (none), `--global` | Install for every session via `~/.claude/settings.json` |
| `--project [DIR]` | Install for one project via `DIR/.claude/settings.local.json` (default: current directory) |
| `--uninstall` | Remove; combine with `--project [DIR]` for a project install |
| `--help` | Usage |

### cache_warm.sh

Reads the status line JSON on stdin, prints one line, always exits 0. It prints nothing when there is nothing to show, so it can never blank out a status line it is embedded in.

| Environment variable | Default | Meaning |
|---|---|---|
| `CACHE_WARM_STYLE` | `short` | `long` prints full sentences: `Prompt cache warm, about 31 min left.` |
| `CACHE_WARM_DEFAULT_TTL` | `3600` | TTL in seconds when neither source reveals the tier |
| `CACHE_WARM_TAIL` | `100` | Transcript lines scanned in the fallback path |
| `NO_COLOR` | unset | Disable ANSI colors |

Colors: green while warm, yellow in the last sixth of the TTL (10 minutes of an hour; never less than 60 seconds), blue when cold.

### usage_forecast.sh

Same contract: status line JSON on stdin, one line out, always exit 0, nothing when the payload has no `rate_limits`.

| Environment variable | Default | Meaning |
|---|---|---|
| `USAGE_FORECAST_STYLE` | `short` | `long` prints full sentences: `5-hour limit 52% used, 3.9× a sustainable pace: runs out about 15:36, resets 19:20.` |
| `USAGE_FORECAST_TOP` | `warn` | When to name the top spender: `warn` (while a limit is red), `always`, or `never` (also skips the transcript scan) |
| `USAGE_FORECAST_WINDOW` | `1800` | Seconds of spending the top spender's share covers |
| `USAGE_FORECAST_ACCOUNT` | `1` | `0` never reads the login or calls the usage endpoint, so no Fable limit is shown |
| `USAGE_FORECAST_ACCOUNT_EVERY` | `300` | Seconds between usage-endpoint fetches |
| `USAGE_FORECAST_KEYCHAIN` | unset | `1` reads the login from the macOS Keychain; macOS may ask once to allow it |
| `USAGE_FORECAST_CACHE_DIR` | `~/.cache/claude-statusline-plus` | Readings, scan results and fetched limits (`$XDG_CACHE_HOME` is honored) |
| `USAGE_FORECAST_PROJECTS` | `~/.claude/projects` | Transcripts to scan (`$CLAUDE_CONFIG_DIR` is honored) |
| `USAGE_FORECAST_TAIL_BYTES` | `16777216` | Bytes read from the end of a large transcript |
| `NO_COLOR` | unset | Disable ANSI colors |

Colors: plain while on track, yellow above `1.0×`, red when the limit runs out before it resets. A `spend_limit` window (behind a Claude apps gateway) shows as `spend 62%`, yellow from 80% and red from 100%.

Set these in the status line command, for example `"command": "USAGE_FORECAST_TOP=always bash ~/.claude/statusline_plus.sh"`.

### statusline_plus.sh

Runs `cache_warm.sh` and `usage_forecast.sh` from its own directory, passing each the same stdin, and joins their output with ` · `. A segment that is not installed, or prints nothing, is skipped.

## Claude Code Integration

This project adds no commands, agents, skills, or hooks. It plugs into one extension point, the `statusLine` setting.

### As the whole status line

This is what the installer configures when you have none:

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash \"/home/you/.claude/statusline_plus.sh\"",
    "refreshInterval": 30
  }
}
```

### Inside an existing status line script

Pass the JSON your script receives on stdin through to each segment and append whatever comes back:

```bash
input=$(cat)     # most status line scripts already do this
cache=$(printf '%s' "$input" | bash ~/.claude/cache_warm.sh 2>/dev/null)
usage=$(printf '%s' "$input" | bash ~/.claude/usage_forecast.sh 2>/dev/null)
echo "your existing output${cache:+ · $cache}${usage:+ · $usage}"
```

Then add `"refreshInterval": 30` to your `statusLine` object so the countdown and the forecast keep up while you are idle.

### Wrapping another status line

If your status line is a command you do not own, such as an `npx` package, wrap it. Save this as `~/.claude/statusline-wrapper.sh` and point `statusLine.command` at it:

```bash
#!/bin/bash
input=$(cat)
line=$(printf '%s' "$input" | npx -y your-statusline-package)
extra=$(printf '%s' "$input" | bash ~/.claude/statusline_plus.sh 2>/dev/null)
echo "$line${extra:+ · $extra}"
```

## Project Structure

```
cache_warm.sh                  prompt cache segment: payload and transcript parsing, rendering
usage_forecast.sh              usage limit segment: pace, forecast, shared readings, transcript scan
statusline_plus.sh             runs both segments and joins their output
install.sh                     installer and uninstaller, global or per-project
tests/test_cache_warm.py       cache segment behavior, driven through stdin and stdout
tests/test_usage_forecast.py   usage segment behavior, the same way
tests/test_install.py          installer behavior against throwaway config directories
.github/workflows/test.yml     pytest on Ubuntu and macOS, plus shellcheck
```

Run the tests with `python -m pytest tests/ -q`. They need `bash`, `jq`, and `pytest`.

The segments are tested through their real interface rather than by unit: every test pipes a payload, and where needed synthetic transcripts and readings, to the script and asserts on the line it prints. Both suites were hardened by mutation testing. The regression tests at the bottom of `test_cache_warm.py` each pin a bug that an earlier version of that suite missed. For `usage_forecast.sh`, 38 deliberately planted bugs were each caught before release: pricing, deduplication, the look-back, the record-only-a-new-high rule, the scan's time and file filters, and the login handling for the Fable limit. The Fable tests run against a local stand-in for the usage endpoint, never the real one.

## Limitations

For the prompt cache:

- **It is an estimate.** The cache lives on Anthropic's servers and cannot be queried. The segment reports when the cache should still be warm; an occasional early miss will not be predicted.
- **It detects expiry and model switches, nothing else.** Other actions also invalidate the cache and are not detected: `/compact` rebuilds the conversation layer, toggling fast mode changes the cache key, and connecting or disconnecting an MCP server invalidates it when that server's tools are loaded into the prompt prefix rather than deferred. After any of these the segment can read warm when the next request will miss.
- **The cold-start figure is an upper bound.** On a miss, the tool-definition and system-prompt layer is often still cached, because other sessions in the same directory keep it warm. The number shown is the full context.
- **Main conversation only.** Subagents and workflows have separate 5-minute caches and are not shown.
- **Model-switch detection covers ordinary `claude-*` model ids.** Provider-specific ids such as Bedrock ARNs are not compared, so that an unfamiliar format can never read as a permanent false "model changed".
- **The transcript fallback depends on an undocumented file format.** On Claude Code 2.1.251 and later it is only used to learn the last model. On older versions a change to the transcript format would make the segment disappear, not mislead.

For the usage forecast:

- **The forecast is "at this pace".** It extrapolates the recent rate in a straight line. Work that stops early makes a red that never arrives, and a burst that starts after a quiet stretch takes a few minutes to show.
- **Readings come from renders.** A status line only sees the meter move after one of its own responses. Usage from headless runs, claude.ai, or another machine shows up the next time an interactive session here gets a response.
- **The top spender is a list-price proxy.** How the limits weigh each model and token type is not published, and this machine's transcripts explained about half of the meter's movement when measured. The share ranks sessions; it does not convert into percentage points.
- **Prices are a table in the script.** A model that is not listed is priced like Opus 5. Update `price` in `usage_forecast.sh` when new models ship.
- **The transcript scan depends on an undocumented file format.** If it changes, the top spender disappears; the percentages, pace, and forecast come from the documented payload and are unaffected.
- **Subscriptions only.** API-key sessions get no `rate_limits`, so the segment prints nothing.
- **The Fable limit comes from an undocumented endpoint.** If it changes or refuses the request, the Fable figure disappears; the 5-hour and weekly limits are unaffected. It is up to 5 minutes old, and it needs `curl`.
- **On macOS the login is in the Keychain.** The Fable figure needs `USAGE_FORECAST_KEYCHAIN=1` there, or `CLAUDE_CODE_OAUTH_TOKEN`.

For both:

- **No native Windows support.** They are bash scripts; use WSL.

## License

MIT. See [LICENSE](LICENSE).
