# claude_cache_statusline

A Claude Code status line segment that shows whether your prompt cache is still warm, how long it has left, and what a cold start will cost.

```
cache warm 31m          next message is a cheap cache read
cache warm 4m           (yellow) expiring soon: send now or accept a cold start
cache cold · 140k       expired; next message re-caches ~140k tokens at full price
cache cold (model changed) · 140k
cache warm 3m (5m ttl)  you are on the 5-minute window, not the 1-hour one
cache off               the API is not reporting any prompt caching
```

## Problem

Every request Claude Code sends includes the entire conversation so far. Prompt caching makes that affordable: a cached prefix is read at a fraction of the normal input price. But the cache expires after a period of inactivity, and nothing in the terminal tells you when.

So you step away from a 300k-token session, come back 65 minutes later, type "ok, continue", and that one short message re-processes all 300k tokens at full price. On a subscription that is a visible bite out of your usage window. The expiry is invisible, and the cost lands on whichever message happens to come next.

The information needed to avoid this exists. It just is not displayed anywhere in the CLI.

## Approach

Claude Code (2.1.251 and later) passes every status line script a `prompt_cache` object containing the cache's expiry time, its TTL, and the number of tokens that would be re-cached if it went cold. This tool renders that as a countdown. On older versions, which lack that object, it reconstructs the same answer from the session transcript.

It also overlays one check the raw expiry does not give you: caches are per-model, so right after `/model` the next request is a full miss even when the clock says there is time left. The segment reports that as cold.

Knowing the state changes what you do:

- **Warm, plenty of time:** carry on.
- **Yellow:** send your next message now, or accept that the following one will be a cold start.
- **Cold:** the full re-cache is coming regardless, so this is the cheapest moment to `/compact` or `/clear` first. Both invalidate the cache anyway; doing them while it is already cold costs nothing extra.

## Installation

Requires `bash` and `jq`. Works on Linux, macOS, and WSL.

```bash
curl -fsSL https://raw.githubusercontent.com/jimmc414/claude_cache_statusline/main/install.sh | bash
```

Or from a clone:

```bash
git clone https://github.com/jimmc414/claude_cache_statusline
cd claude_cache_statusline
./install.sh
```

What the installer does:

- Copies `cache_warm.sh` into your Claude Code config directory (`~/.claude`, or `$CLAUDE_CONFIG_DIR`).
- **If you have no status line,** configures one in `settings.json`, after backing the file up to `settings.json.bak-cache-warm`. Your other settings are preserved.
- **If you already have a status line, it changes nothing in your settings.** It prints the two lines to add to your existing script instead (see [Claude Code Integration](#claude-code-integration)).

### Global or per-project

```bash
./install.sh                    # every session (default): ~/.claude/settings.json
./install.sh --project          # only the project in the current directory
./install.sh --project ~/code/myapp
```

`--project` installs into that project's `.claude/` directory and writes `.claude/settings.local.json`, which is the personal, uncommitted settings file. A project-level status line replaces the status line from any broader scope, so if you already have a custom one globally or in the project's shared `.claude/settings.json`, the installer will not shadow it and prints the manual steps instead.

With the one-liner, pass options after `bash -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/jimmc414/claude_cache_statusline/main/install.sh | bash -s -- --project
```

### Uninstall

```bash
./install.sh --uninstall
./install.sh --uninstall --project ~/code/myapp
```

Removes the script, and removes the `statusLine` entry only if it is the one this installer wrote.

## Usage

Nothing to run. After installing, the segment appears once the session has made its first API call, and disappears again if there is nothing to report.

To try it by hand, pipe it a payload:

```bash
echo '{"prompt_cache":{"caching_observed":true,"ttl":"1h","expires_at":'$(( $(date +%s) + 1860 ))'}}' \
  | bash ~/.claude/cache_warm.sh
# cache warm 31m
```

## How It Works

### What "warm" means

Caching is prefix-based. Each request's prompt is the tool definitions, the system prompt, and the full conversation history. On a hit, the API reads the entire matching prefix from cache and refreshes the expiry of all of it; only the new suffix is written. So the whole conversation stays warm together, the window slides forward with every request, and old turns do not age out while you keep working.

The TTL is measured from when a request is sent, not from when the response finishes. A response that takes four minutes to generate leaves 56 minutes, not 60.

### Which TTL you get

Claude Code requests a 1-hour TTL for the main conversation on a Claude subscription within its included usage, and 5 minutes otherwise: API keys, Bedrock, Vertex, Foundry, or a subscription that has moved onto usage credits. The segment reads the TTL rather than assuming it, and labels the 5-minute case `(5m ttl)` so a silent drop from the 1-hour window is visible.

### Sources, in order

1. **`prompt_cache` in the status line payload** (Claude Code 2.1.251+). Used as-is. The CLI stamps it when each request is dispatched, which matches how the API measures the TTL, and it records cache touches that never reach the transcript, such as a forked agent reading the main conversation's cache.
2. **The session transcript** (older versions). Each main-chain assistant entry records an API call's `usage`, including `cache_creation.ephemeral_1h_input_tokens` versus `ephemeral_5m_input_tokens`, which reveals the TTL tier. Expiry is the last request's start time plus the TTL.

Details that matter in the transcript path:

- One API call is written as several lines, one per content block, sharing a message id. With extended thinking the first block can land minutes after the request was sent, so the anchor is the user or tool-result entry that triggered the call, not the response.
- API-error and `<synthetic>` entries carry a zeroed usage block and are ignored.
- Subagent calls live in separate transcript files and use their own 5-minute caches. They do not keep the main conversation warm and are not counted.
- Only the tail of the transcript is read, so cost stays flat as sessions grow. Transcript lines can approach a megabyte each, so the fallback scan is bounded in bytes, not lines.

### How sharp the expiry is

Measured across roughly 35,000 main-conversation API calls from one machine's transcripts, comparing the idle gap before each call with whether it hit the cache: the longest gap that still hit was 59.84 minutes, and the shortest that missed was 60.46 minutes. Nothing hit past the hour.

Before the hour it is not a guarantee. Gaps of 55 to 60 minutes hit 4 times out of 5, and about 0.4% of calls missed after short gaps for reasons not visible in the transcript. That is why the segment turns yellow for the last sixth of the window rather than pretending the final minutes are safe, and why minutes are rounded down.

### Staying current while idle

A status line only re-runs on events, so a countdown would freeze exactly when you stop typing. Two things prevent that:

- `statusLine.refreshInterval` re-runs the command every N seconds. The installer sets it to 30.
- Claude Code schedules a re-run for the moment `prompt_cache.expires_at` passes, so the change from warm to cold repaints on its own even without a refresh interval.

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

Set these in the status line command, for example `"command": "CACHE_WARM_STYLE=long bash ~/.claude/cache_warm.sh"`.

Colors: green while warm, yellow in the last sixth of the TTL (10 minutes of an hour; never less than 60 seconds), blue when cold.

## Claude Code Integration

This project adds no commands, agents, skills, or hooks. It plugs into one extension point, the `statusLine` setting.

### As the whole status line

This is what the installer configures when you have none:

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash \"/home/you/.claude/cache_warm.sh\"",
    "refreshInterval": 30
  }
}
```

### Inside an existing status line script

Pass the JSON your script receives on stdin through to `cache_warm.sh` and append whatever comes back:

```bash
input=$(cat)     # most status line scripts already do this
cache=$(printf '%s' "$input" | bash ~/.claude/cache_warm.sh 2>/dev/null)
echo "your existing output${cache:+ · $cache}"
```

Then add `"refreshInterval": 30` to your `statusLine` object so the countdown keeps ticking while you are idle.

### Wrapping another status line

If your status line is a command you do not own, such as an `npx` package, wrap it. Save this as `~/.claude/statusline-wrapper.sh` and point `statusLine.command` at it:

```bash
#!/bin/bash
input=$(cat)
line=$(printf '%s' "$input" | npx -y your-statusline-package)
cache=$(printf '%s' "$input" | bash ~/.claude/cache_warm.sh 2>/dev/null)
echo "$line${cache:+ · $cache}"
```

## Project Structure

```
cache_warm.sh                the segment: payload and transcript parsing, rendering
install.sh                   installer and uninstaller, global or per-project
tests/test_cache_warm.py     segment behavior, driven through stdin and stdout
tests/test_install.py        installer behavior against throwaway config directories
.github/workflows/test.yml   pytest on Ubuntu and macOS, plus shellcheck
```

Run the tests with `python -m pytest tests/ -q`. They need `bash`, `jq`, and `pytest`.

The segment is tested through its real interface rather than by unit: every test pipes a payload and a synthetic transcript to the script and asserts on the line it prints. The suite was hardened by mutation testing; each regression test at the bottom of `test_cache_warm.py` pins a deliberately introduced bug that an earlier version of the suite failed to catch.

## Limitations

- **It is an estimate.** The cache lives on Anthropic's servers and cannot be queried. The segment reports when the cache should still be warm; an occasional early miss will not be predicted.
- **It detects expiry and model switches, nothing else.** Other actions also invalidate the cache and are not detected: `/compact` rebuilds the conversation layer, toggling fast mode changes the cache key, and connecting or disconnecting an MCP server invalidates it when that server's tools are loaded into the prompt prefix rather than deferred. After any of these the segment can read warm when the next request will miss.
- **The cold-start figure is an upper bound.** On a miss, the tool-definition and system-prompt layer is often still cached, because other sessions in the same directory keep it warm. The number shown is the full context.
- **Main conversation only.** Subagents and workflows have separate 5-minute caches and are not shown.
- **Model-switch detection covers ordinary `claude-*` model ids.** Provider-specific ids such as Bedrock ARNs are not compared, so that an unfamiliar format can never read as a permanent false "model changed".
- **The transcript fallback depends on an undocumented file format.** On Claude Code 2.1.251 and later it is only used to learn the last model. On older versions a change to the transcript format would make the segment disappear, not mislead.
- **No native Windows support.** It is a bash script; use WSL.

## License

MIT. See [LICENSE](LICENSE).
