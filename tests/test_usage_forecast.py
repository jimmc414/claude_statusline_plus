"""Tests for usage_forecast.sh, driven through its real stdin/stdout interface."""

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "usage_forecast.sh"
HOUR = 3600
DAY = 24 * HOUR
FIVE_H = 5 * HOUR
WEEK = 7 * DAY
S5 = 1_790_000_400        # a 5-hour window opens Mon 14:20 UTC...
R5 = S5 + FIVE_H          # ...and resets at 19:20 UTC
R7 = S5 + 3 * DAY         # the weekly window resets Thu 14:20 UTC


def utc(epoch, fmt="%H:%M"):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(fmt)


def limits(u5=None, u7=None, r5=R5, r7=R7, spend=None, spend_reset=None):
    rl = {}
    if u5 is not None:
        rl["five_hour"] = {"used_percentage": u5, "resets_at": r5}
    if u7 is not None:
        rl["seven_day"] = {"used_percentage": u7, "resets_at": r7}
    if spend is not None:
        rl["spend_limit"] = {"used_percentage": spend, "resets_at": spend_reset}
    return {"rate_limits": rl}


class Runner:
    """Pipes a payload to the script with a throwaway cache and projects directory."""

    def __init__(self, tmp_path):
        self.cache = tmp_path / "cache"
        self.projects = tmp_path / "projects"
        self.config = tmp_path / "claude"

    def __call__(self, payload, now, stdin=None, **env):
        # The account fetch is off unless a test turns it on, and the Claude config
        # directory is a throwaway one, so no test can ever read a real login.
        full_env = {**os.environ, "NO_COLOR": "1", "TZ": "UTC", "USAGE_FORECAST_NOW": str(now),
                    "USAGE_FORECAST_CACHE_DIR": str(self.cache), "USAGE_FORECAST_PROJECTS": str(self.projects),
                    "USAGE_FORECAST_SYNC": "1", "USAGE_FORECAST_ACCOUNT": "0", "USAGE_FORECAST_COUNTDOWN": "0",
                    "CLAUDE_CONFIG_DIR": str(self.config), "CLAUDE_CODE_OAUTH_TOKEN": None,
                    "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost", **env}
        for key in [k for k, v in full_env.items() if v is None]:
            del full_env[key]
        proc = subprocess.run(["bash", str(SCRIPT)], text=True, capture_output=True, env=full_env,
                              input=json.dumps(payload) if stdin is None else stdin, timeout=30)
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr == ""
        return proc.stdout.strip()


@pytest.fixture
def run(tmp_path):
    return Runner(tmp_path)


def seed(run, *rows):
    """Pre-record meter readings: (epoch, 5h used, 5h reset, 7d used, 7d reset)."""
    run.cache.mkdir(parents=True, exist_ok=True)
    with open(run.cache / "usage_samples.tsv", "a") as f:
        for row in rows:
            f.write("\t".join("-" if v is None else str(v) for v in row) + "\n")


def readings(run):
    path = run.cache / "usage_samples.tsv"
    return [line.split("\t") for line in path.read_text().splitlines()] if path.exists() else []


# =============================================================================
# The forecast
# =============================================================================

def test_on_track_shows_plain_usage(run):
    assert run(limits(u5=12, u7=41), now=S5 + 2 * HOUR) == "5h 12% · 7d 41%"


def test_only_the_windows_present_are_shown(run):
    assert run(limits(u7=41), now=S5 + 2 * HOUR) == "7d 41%"


def test_the_night_a_five_hour_limit_ran_out(run):
    # 52% gone 40 minutes into the window: 3.9x the pace that lasts 5 hours, so
    # the rest runs out ~37 minutes later, hours before the reset.
    now = S5 + 40 * 60
    assert run(limits(u5=52, u7=38), now=now) == "5h 52% 3.9× cap 15:36 (resets 19:20) · 7d 38%"


def test_long_style_says_it_in_sentences(run):
    out = run(limits(u5=52, u7=38), now=S5 + 40 * 60, USAGE_FORECAST_STYLE="long")
    assert out == ("5-hour limit 52% used, 3.9× a sustainable pace: runs out about 15:36, resets 19:20. "
                   "Weekly limit 38% used.")


def test_running_out_is_red(run):
    # The percentage is colored by its level (52%: green); the forecast is red.
    out = run(limits(u5=52), now=S5 + 40 * 60, NO_COLOR=None)
    assert "\033[38;5;40m52%\033[0m \033[38;5;196m3.9× cap 15:36\033[0m" in out


def test_average_pace_over_one_always_runs_out_first(run):
    # 45% in 2 hours is 1.1x: 112% by the reset, so the cap comes first (18:46).
    assert run(limits(u5=45), now=S5 + 2 * HOUR) == "5h 45% 1.1× cap 18:46 (resets 19:20)"


def test_burning_fast_from_a_low_base_is_yellow_without_a_cap(run):
    # The last half hour went 8% -> 20%: 1.2x. At that rate the other 80% takes
    # 3.3 hours, and the window resets in 2.
    now = S5 + 3 * HOUR
    seed(run, (S5 + HOUR, 8, R5, None, None), (now - 5 * 60, 20, R5, None, None))
    assert run(limits(u5=20), now=now) == "5h 20% 1.2×"
    assert "\033[38;5;40m20%\033[0m \033[38;5;220m1.2×\033[0m" in run(limits(u5=20), now=now, NO_COLOR=None)


def test_used_up(run):
    assert run(limits(u5=100), now=S5 + 3 * HOUR) == "5h 100% capped (resets 19:20)"


def test_nearly_used_up_never_rounds_to_a_full_hundred(run):
    assert run(limits(u5=99.7), now=S5 + int(4.9 * HOUR)).startswith("5h 99% ")


def test_no_pace_is_guessed_in_the_first_minutes_of_a_window(run):
    assert run(limits(u5=10), now=S5 + 5 * 60) == "5h 10%"


def test_weekly_times_carry_the_day(run):
    # 80% after 4 of 7 days is 1.4x; the last 20% goes in one more day.
    now = R7 - 3 * DAY
    out = run(limits(u7=80), now=now)
    assert out == f"7d 80% 1.4× cap {utc(now + DAY, '%a %H:%M')} (resets {utc(R7, '%a %H:%M')})"
    assert out == "7d 80% 1.4× cap Tue 14:20 (resets Thu 14:20)"


def test_spend_limit(run):
    reset = 1_790_000_000 + 10 * DAY
    assert run(limits(spend=62, spend_reset=reset), now=S5) == "spend 62%"
    assert run(limits(spend=104, spend_reset=reset), now=S5) == f"spend 104% (resets {utc(reset, '%b %d')})"


# --- the 5-hour countdown and level colors ---------------------------------------

GREEN, YELLOW, RED = "\033[38;5;40m", "\033[38;5;220m", "\033[38;5;196m"


@pytest.mark.parametrize("u5,now,expected", [
    (12, S5 + 2 * HOUR, "5h 12% (resets in 3h00m)"),
    (12, S5 + 2 * HOUR - 7 * 60, "5h 12% (resets in 3h07m)"),
    (80, R5 - 52 * 60, "5h 80% (resets in 52m)"),
    (92, R5 - 30, "5h 92% (resets in <1m)"),
    (52, S5 + 40 * 60, "5h 52% 3.9× cap 15:36 (resets in 4h20m)"),
    (100, S5 + 3 * HOUR, "5h 100% capped (resets in 2h00m)"),
])
def test_the_five_hour_limit_counts_down_to_its_reset(run, u5, now, expected):
    assert run(limits(u5=u5, u7=41), now=now, USAGE_FORECAST_COUNTDOWN=None) == expected + " · 7d 41%"


def test_a_fast_five_hour_pace_keeps_its_countdown(run):
    now = S5 + 3 * HOUR
    seed(run, (S5 + HOUR, 8, R5, None, None), (now - 5 * 60, 20, R5, None, None))
    assert run(limits(u5=20), now=now, USAGE_FORECAST_COUNTDOWN=None) == "5h 20% 1.2× (resets in 2h00m)"


def test_long_style_counts_down_too(run):
    assert run(limits(u5=12), now=S5 + 2 * HOUR, USAGE_FORECAST_COUNTDOWN=None,
               USAGE_FORECAST_STYLE="long") == "5-hour limit 12% used, resets in 3h00m."
    assert run(limits(u5=52), now=S5 + 40 * 60, USAGE_FORECAST_COUNTDOWN=None, USAGE_FORECAST_STYLE="long") == (
        "5-hour limit 52% used, 3.9× a sustainable pace: runs out about 15:36, resets in 4h20m.")


def test_the_countdown_can_be_turned_off(run):
    assert run(limits(u5=12), now=S5 + 2 * HOUR, USAGE_FORECAST_COUNTDOWN="0") == "5h 12%"


@pytest.mark.parametrize("u5,color", [(12, GREEN), (74, GREEN), (75, YELLOW), (89, YELLOW), (90, RED), (97, RED)])
def test_the_five_hour_percentage_turns_yellow_then_red(run, u5, color):
    # Late in the window, so none of these is burning fast enough to add a forecast.
    out = run(limits(u5=u5), now=R5 - 10 * 60, NO_COLOR=None)
    assert f"{color}{u5}%\033[0m" in out


def test_the_color_levels_can_be_tuned(run):
    out = run(limits(u5=60), now=R5 - 10 * 60, NO_COLOR=None, USAGE_FORECAST_LEVELS="50,80")
    assert f"{YELLOW}60%" in out


@pytest.mark.parametrize("levels", ["90,75", "abc", "0,50", "75,200", "75"])
def test_unusable_color_levels_fall_back_to_75_and_90(run, levels):
    out = run(limits(u5=80), now=R5 - 10 * 60, NO_COLOR=None, USAGE_FORECAST_LEVELS=levels)
    assert f"{YELLOW}80%" in out


def test_the_weekly_limit_is_not_colored_by_level(run):
    # 80% an hour before the weekly reset is on track: no forecast color, and no
    # level color either, since only the 5-hour percentage gets those.
    out = run(limits(u7=80), now=R7 - HOUR, NO_COLOR=None)
    assert out == "\033[2m7d\033[0m 80%"


# --- where the rate comes from ------------------------------------------------

def test_recent_readings_override_the_window_average(run):
    # 30% over 3 hours averages 0.5x, but the last half hour went from 5% to 30%:
    # 2.5x, with the rest gone in 84 minutes, before the 19:20 reset.
    now = S5 + 3 * HOUR
    seed(run, (S5 + HOUR, 5, R5, None, None), (now - 20 * 60, 10, R5, None, None))
    assert run(limits(u5=30), now=now) == "5h 30% 2.5× cap 18:44 (resets 19:20)"


def test_readings_from_an_earlier_window_are_ignored(run):
    now = S5 + 3 * HOUR
    seed(run, (S5 - HOUR, 5, R5 - FIVE_H, None, None), (now - 20 * 60, 10, R5 - FIVE_H, None, None))
    assert run(limits(u5=30), now=now) == "5h 30%"


def test_an_account_gone_quiet_calms_down(run):
    # The window average says 1.0x, but nothing has moved for two hours.
    now = S5 + 3 * HOUR
    seed(run, (S5 + 30 * 60, 20, R5, None, None), (now - 2 * HOUR, 60, R5, None, None))
    assert run(limits(u5=60), now=now) == "5h 60%"


def test_a_stale_session_shows_the_freshest_reading(run):
    # This session's last response saw 40%; another session has since seen 55%.
    now = S5 + 4 * HOUR
    seed(run, (now - 60, 55, R5, None, None))
    assert run(limits(u5=40), now=now) == "5h 55%"


def test_records_a_reading_only_when_it_raises_the_maximum(run):
    now = S5 + 2 * HOUR
    run(limits(u5=20, u7=30), now=now)
    assert readings(run) == [[str(now), "20", str(R5), "30", str(R7)]]
    run(limits(u5=20, u7=30), now=now + 30)            # unchanged: nothing new
    run(limits(u5=18, u7=30), now=now + 60)            # a stale session: lower
    assert len(readings(run)) == 1
    run(limits(u5=25, u7=30), now=now + 90)            # 5h rose, 7d did not
    assert readings(run)[-1] == [str(now + 90), "25", str(R5), "-", "-"]
    run(limits(u5=25, u7=31), now=now + 120)
    assert readings(run)[-1] == [str(now + 120), "-", "-", "31", str(R7)]


def test_a_new_window_starts_a_new_record(run):
    run(limits(u5=90), now=S5 + 4 * HOUR)
    run(limits(u5=3, r5=R5 + FIVE_H), now=R5 + 10)
    assert readings(run)[-1][1:3] == ["3", str(R5 + FIVE_H)]


def test_the_readings_file_stays_small(run):
    seed(run, *[(S5 + i, 1, R5 - FIVE_H, None, None) for i in range(5000)])
    run(limits(u5=20), now=S5 + 2 * HOUR)
    rows = readings(run)
    assert 1 <= len(rows) <= 1000
    assert rows[-1][1] == "20"


# --- bad input ----------------------------------------------------------------

def test_no_rate_limits_prints_nothing(run):
    assert run({"model": {"id": "claude-opus-5-5"}}, now=S5) == ""
    assert not run.cache.exists()                       # and touches nothing


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", "null", '{"rate_limits": 7}'])
def test_garbage_stdin_prints_nothing(run, stdin):
    assert run(None, now=S5, stdin=stdin) == ""


@pytest.mark.parametrize("window", [
    {"used_percentage": -5, "resets_at": R5},
    {"used_percentage": "12", "resets_at": R5},
    {"used_percentage": 1e9, "resets_at": R5},
    {"used_percentage": 12, "resets_at": S5 - 1},       # already reset
    {"used_percentage": 12, "resets_at": R5 + 30 * DAY},  # further out than a window lasts
    {"used_percentage": 12},
    "12%",
])
def test_a_malformed_window_is_left_out(run, window):
    payload = {"rate_limits": {"five_hour": window, "seven_day": {"used_percentage": 41, "resets_at": R7}}}
    assert run(payload, now=S5 + HOUR) == "7d 41%"


@pytest.mark.parametrize("now", ["", "abc", "-5", "1e9"])
def test_garbage_clock_falls_back_to_the_real_one(run, now):
    real = int(time.time())
    out = run(limits(u5=12, r5=real + HOUR), now=0, USAGE_FORECAST_NOW=now)
    assert out == "5h 12%"


def test_an_unwritable_cache_still_renders(run, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    out = run(limits(u5=52), now=S5 + 40 * 60, USAGE_FORECAST_CACHE_DIR=str(blocker / "cache"))
    assert out == "5h 52% 3.9× cap 15:36 (resets 19:20)"


# =============================================================================
# Who is spending: the transcript scan
# =============================================================================

NOW = S5 + 40 * 60   # the red scenario from above
RED = "5h 52% 3.9× cap 15:36 (resets 19:20)"
SID_A = "aaaaaaaa-1111-4111-8111-111111111111"
SID_B = "bbbbbbbb-2222-4222-8222-222222222222"


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123Z")


def call(at, msg_id, model="claude-opus-5-5", inp=0, out=0, read=0, w1h=0, w5m=0, block="ok"):
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
             "cache_creation_input_tokens": w1h + w5m,
             "cache_creation": {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": w5m}}
    return {"type": "assistant", "timestamp": iso(at), "sessionId": "x",
            "message": {"id": msg_id, "model": model, "role": "assistant", "usage": usage,
                        "content": [{"type": "text", "text": block}]}}


def title(text):
    return {"type": "ai-title", "aiTitle": text, "sessionId": "x"}


def transcript(run, sid, entries, sub=None, project="-home-me-app", age=None, compact=False):
    base = run.projects / project
    path = base / f"{sid}.jsonl" if sub is None else base / sid / "subagents" / f"{sub}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    seps = (",", ":") if compact else None     # Claude Code writes compact JSON
    path.write_text("".join(json.dumps(e, separators=seps) + "\n" for e in entries))
    if age is not None:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


def two_sessions(run):
    # A: one Opus 5.5 call, 1M output tokens = $20.
    # B: one Fable 5.1 call (1M output = $50) plus a subagent call (200k output = $10).
    transcript(run, SID_A, [title("fix flaky test"), call(NOW - 60, "msg_a1", out=1_000_000)])
    transcript(run, SID_B, [title("refactor auth middleware"),
                            call(NOW - 120, "msg_b1", model="claude-fable-5-1", out=1_000_000)])
    transcript(run, SID_B, [call(NOW - 90, "msg_b2", model="claude-fable-5-1", out=200_000)], sub="agent-x")


def test_top_spender_is_named_while_a_limit_is_running_out(run):
    two_sessions(run)
    payload = {**limits(u5=52), "session_id": SID_A}
    assert run(payload, now=NOW) == RED + " · top: refactor auth middleware 75%"


def test_this_session_when_it_is_the_top_spender(run):
    two_sessions(run)
    assert run({**limits(u5=52), "session_id": SID_B}, now=NOW) == RED + " · top: this session 75%"


def test_blocks_of_one_call_count_once(run):
    # B's call is written as three lines (one per content block) sharing an id.
    transcript(run, SID_A, [title("a"), call(NOW - 60, "msg_a1", out=1_000_000)])
    transcript(run, SID_B, [title("b")] + [call(NOW - 120, "msg_b1", out=600_000, block=str(i)) for i in range(3)])
    assert run({**limits(u5=52), "session_id": SID_B}, now=NOW) == RED + " · top: a 63%"   # 20 of 32


def test_calls_before_the_window_do_not_count(run):
    transcript(run, SID_A, [title("a"), call(NOW - 60, "msg_a1", out=100_000)])
    transcript(run, SID_B, [title("b"), call(NOW - 2 * HOUR, "msg_b1", out=5_000_000),
                            call(NOW - 60, "msg_b2", out=50_000)])
    assert run({**limits(u5=52), "session_id": SID_B}, now=NOW) == RED + " · top: a 67%"


def test_cache_reads_are_priced_per_model(run):
    # 4M cache-read tokens: $1.00 on Fable 5.1 ($0.25/M), $2.00 on Opus 5 ($0.50/M).
    transcript(run, SID_A, [title("a"), call(NOW - 60, "m1", model="claude-fable-5-1", read=4_000_000)])
    transcript(run, SID_B, [title("b"), call(NOW - 60, "m2", model="claude-opus-5", read=4_000_000)])
    assert run({**limits(u5=52), "session_id": SID_A}, now=NOW) == RED + " · top: b 67%"


def test_cache_writes_are_priced_by_tier(run):
    # Opus 5 input $5/M: 1M one-hour writes = $10, 1M five-minute writes = $6.25.
    transcript(run, SID_A, [title("a"), call(NOW - 60, "m1", model="claude-opus-5", w1h=1_000_000)])
    transcript(run, SID_B, [title("b"), call(NOW - 60, "m2", model="claude-opus-5", w5m=1_000_000)])
    assert run({**limits(u5=52), "session_id": SID_B}, now=NOW) == RED + " · top: a 62%"


def test_no_top_with_a_single_session(run):
    transcript(run, SID_A, [title("a"), call(NOW - 60, "m1", out=1_000_000)])
    assert run({**limits(u5=52), "session_id": SID_A}, now=NOW) == RED


def test_top_waits_for_a_limit_to_be_running_out(run):
    two_sessions(run)
    assert run({**limits(u5=12), "session_id": SID_A}, now=NOW + 3 * HOUR) == "5h 12%"


def test_top_always_and_never(run):
    two_sessions(run)
    payload = {**limits(u5=12), "session_id": SID_A}
    assert run(payload, now=NOW, USAGE_FORECAST_TOP="always") == "5h 12% · top: refactor auth middleware 75%"
    assert run({**limits(u5=52), "session_id": SID_A}, now=NOW, USAGE_FORECAST_TOP="never") == RED


def test_a_sessions_name_beats_its_ai_title(run):
    two_sessions(run)
    path = run.projects / "-home-me-app" / f"{SID_B}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps({"type": "agent-name", "agentName": "auth-rework", "sessionId": SID_B}) + "\n")
    assert run({**limits(u5=52), "session_id": SID_A}, now=NOW) == RED + " · top: auth-rework 75%"


@pytest.mark.parametrize("compact", [True, False])
def test_a_title_far_back_in_a_large_transcript_is_found(run, compact):
    filler = [{"type": "user", "timestamp": iso(NOW - 3000), "message": {"content": "x" * 900}}] * 40
    transcript(run, SID_A, [title("a"), call(NOW - 60, "m1", out=100_000)])
    transcript(run, SID_B, [title("long session")] + filler + [call(NOW - 60, "m2", out=1_000_000)],
               compact=compact)
    out = run({**limits(u5=52), "session_id": SID_A}, now=NOW, USAGE_FORECAST_TAIL_BYTES="4096")
    assert out == RED + " · top: long session 91%"


def test_a_session_without_any_title_is_named_by_its_id(run):
    transcript(run, SID_A, [title("a"), call(NOW - 60, "m1", out=100_000)])
    transcript(run, SID_B, [call(NOW - 60, "m2", out=1_000_000)])
    assert run({**limits(u5=52), "session_id": SID_A}, now=NOW) == RED + " · top: session bbbbbbbb 91%"


def test_long_titles_are_shortened(run):
    transcript(run, SID_A, [title("a"), call(NOW - 60, "m1", out=100_000)])
    long_title = "an extremely long title that goes on and on"
    transcript(run, SID_B, [title(long_title), call(NOW - 60, "m2", out=1_000_000)])
    out = run({**limits(u5=52), "session_id": SID_A}, now=NOW)
    assert out == RED + f" · top: {long_title[:27]}… 91%"


def test_control_characters_in_a_title_are_dropped(run):
    transcript(run, SID_A, [title("a"), call(NOW - 60, "m1", out=100_000)])
    transcript(run, SID_B, [title("evil\u001b[31mred"), call(NOW - 60, "m2", out=1_000_000)])
    out = run({**limits(u5=52), "session_id": SID_A}, now=NOW, NO_COLOR=None)
    assert "evil[31mred" in out and "\u001b[31m" not in out


def test_transcripts_untouched_for_longer_than_the_window_are_skipped(run):
    transcript(run, SID_A, [title("a"), call(NOW - 60, "m1", out=100_000)])
    transcript(run, SID_B, [title("b"), call(NOW - 60, "m2", out=1_000_000)], age=2 * HOUR)
    assert run({**limits(u5=52), "session_id": SID_A}, now=NOW) == RED


def test_synthetic_and_api_error_entries_do_not_count(run):
    transcript(run, SID_A, [title("a"), call(NOW - 60, "m1", out=100_000)])
    fake = call(NOW - 60, "m2", model="<synthetic>", out=5_000_000)
    err = {**call(NOW - 60, "m3", out=5_000_000), "isApiErrorMessage": True}
    transcript(run, SID_B, [title("b"), fake, err])
    assert run({**limits(u5=52), "session_id": SID_A}, now=NOW) == RED


def test_the_scan_runs_in_the_background_without_blocking(run):
    two_sessions(run)
    payload = {**limits(u5=52), "session_id": SID_A}
    started = time.time()
    first = run(payload, now=NOW, USAGE_FORECAST_SYNC=None)
    assert first == RED                                   # nothing scanned yet
    assert time.time() - started < 10
    fleet = run.cache / "fleet.tsv"
    deadline = time.time() + 20
    while not fleet.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert fleet.exists()
    assert run(payload, now=NOW, USAGE_FORECAST_SYNC=None) == RED + " · top: refactor auth middleware 75%"


def test_a_stuck_scan_lock_is_taken_over(run):
    two_sessions(run)
    lock = run.cache / "fleet.lock"
    lock.mkdir(parents=True)
    stamp = time.time() - 600
    os.utime(lock, (stamp, stamp))
    assert run({**limits(u5=52), "session_id": SID_A}, now=NOW) == RED + " · top: refactor auth middleware 75%"
    assert not lock.exists()


def test_a_live_scan_lock_is_respected(run):
    two_sessions(run)
    (run.cache / "fleet.lock").mkdir(parents=True)
    assert run({**limits(u5=52), "session_id": SID_A}, now=NOW) == RED


def test_an_old_scan_is_not_shown(run):
    run.cache.mkdir(parents=True)
    (run.cache / "fleet.tsv").write_text(f"#\t{NOW - 3600}\t1800\n{SID_B}\t60\t0\tb\n{SID_A}\t20\t0\ta\n")
    out = run({**limits(u5=52), "session_id": SID_A}, now=NOW, USAGE_FORECAST_TOP="warn",
              USAGE_FORECAST_PROJECTS=str(run.projects / "missing"))
    assert out == RED


# =============================================================================
# Model-scoped limits (Fable) from the account usage endpoint
# =============================================================================

class _UsageEndpoint(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append({k.lower(): v for k, v in self.headers.items()})
        status, body = self.server.reply
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def usage_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UsageEndpoint)
    server.requests = []
    server.reply = (200, "{}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


AT = S5 + 2 * HOUR      # Mon 16:20 UTC; the weekly window opened 98 hours ago


def endpoint_time(epoch, micros="708782"):
    """Reset times as the endpoint writes them: microseconds that vary, and +00:00."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(f"%Y-%m-%dT%H:%M:%S.{micros}+00:00")


def usage_reply(fable=None, micros="708782", extra=(), late=0):
    limits = [
        {"kind": "session", "group": "session", "percent": 12, "severity": "normal",
         "resets_at": endpoint_time(R5 - 1, micros), "scope": None, "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": 41, "severity": "normal",
         "resets_at": endpoint_time(R7 - 1, micros), "scope": None, "is_active": False},
    ]
    if fable is not None:
        limits.append({"kind": "weekly_scoped", "group": "weekly", "percent": fable, "severity": "warning",
                       "resets_at": endpoint_time(R7 - 1 + late, micros),
                       "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                       "is_active": False})
    limits.extend(extra)
    return 200, json.dumps({"five_hour": {"utilization": 12.0}, "seven_day_omelette": None, "limits": limits})


def login(run, now, token="tok-123", expires_in=HOUR):
    run.config.mkdir(parents=True, exist_ok=True)
    (run.config / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": "ref-456", "expiresAt": (now + expires_in) * 1000,
        "subscriptionType": "max"}}))


def with_account(api, **env):
    return {"USAGE_FORECAST_ACCOUNT": "1",
            "USAGE_FORECAST_ACCOUNT_URL": f"http://127.0.0.1:{api.server_port}/api/oauth/usage", **env}


def fable_red(u, now=AT):
    # The window average over the 98 hours since the weekly window opened.
    elapsed = now - (R7 - WEEK)
    rate = u / elapsed
    pace = round(rate * WEEK / 100 + 1e-9, 1)
    eta = now + (100 - u) / rate
    return f"Fable {u}% {pace}× cap {utc(eta, '%a %H:%M')} (resets {utc(R7, '%a %H:%M')})"


def test_the_fable_limit_shows_after_the_weekly_one(run, usage_api):
    login(run, AT)
    usage_api.reply = usage_reply(fable=50)
    assert run(limits(u5=12, u7=41), now=AT, **with_account(usage_api)) == "5h 12% · 7d 41% · Fable 50%"


def test_the_fable_limit_can_run_out_first(run, usage_api):
    login(run, AT)
    usage_api.reply = usage_reply(fable=78)
    out = run(limits(u5=12, u7=41), now=AT, **with_account(usage_api))
    assert out == "5h 12% · 7d 41% · " + fable_red(78)
    assert out.endswith("1.3× cap Tue 19:58 (resets Thu 14:20)")


def test_long_style_names_the_fable_limit(run, usage_api):
    login(run, AT)
    usage_api.reply = usage_reply(fable=50)
    out = run(limits(u5=12, u7=41), now=AT, USAGE_FORECAST_STYLE="long", **with_account(usage_api))
    assert out == "5-hour limit 12% used. Weekly limit 41% used. Fable weekly limit 50% used."


def test_fable_readings_give_a_recent_pace(run, usage_api):
    # 50% -> 60% in two hours is 5% an hour: 8.4x a week's worth. The two fetches
    # report the reset a moment apart, across a second boundary, as real ones can.
    login(run, AT, expires_in=DAY)
    usage_api.reply = usage_reply(fable=50, micros="999000")
    run(limits(u5=12, u7=41), now=AT, **with_account(usage_api))
    usage_api.reply = usage_reply(fable=60, micros="001000", late=1)
    out = run(limits(u5=12, u7=41), now=AT + 2 * HOUR, **with_account(usage_api))
    assert out.endswith(f"Fable 60% 8.4× cap {utc(AT + 10 * HOUR, '%a %H:%M')} (resets Thu 14:20)")


def test_a_fable_reading_is_recorded_only_when_it_rises(run, usage_api):
    login(run, AT, expires_in=DAY)
    for offset, fable in ((0, 50), (300, 50), (600, 49), (900, 51)):
        usage_api.reply = usage_reply(fable=fable)
        run(limits(u5=12, u7=41), now=AT + offset, **with_account(usage_api))
    rows = [line.split("\t") for line in (run.cache / "scoped_samples.tsv").read_text().splitlines()]
    assert [(r[0], r[1], r[2]) for r in rows] == [(str(AT), "Fable", "50"), (str(AT + 900), "Fable", "51")]
    assert len(usage_api.requests) == 4


def test_the_login_token_goes_in_a_header_and_nowhere_else(run, usage_api):
    login(run, AT)
    usage_api.reply = usage_reply(fable=50)
    run(limits(u5=12, u7=41), now=AT, **with_account(usage_api))
    assert len(usage_api.requests) == 1
    assert usage_api.requests[0]["authorization"] == "Bearer tok-123"
    assert usage_api.requests[0]["anthropic-beta"] == "oauth-2025-04-20"
    for path in run.cache.rglob("*"):
        if path.is_file():
            assert "tok-123" not in path.read_text(errors="replace")
            assert "ref-456" not in path.read_text(errors="replace")


def test_an_expired_login_is_not_used(run, usage_api):
    login(run, AT, expires_in=-10)
    usage_api.reply = usage_reply(fable=50)
    assert run(limits(u5=12, u7=41), now=AT, **with_account(usage_api)) == "5h 12% · 7d 41%"
    assert usage_api.requests == []


def test_an_explicit_oauth_token_wins(run, usage_api):
    login(run, AT)
    usage_api.reply = usage_reply(fable=50)
    run(limits(u5=12, u7=41), now=AT, CLAUDE_CODE_OAUTH_TOKEN="env-tok", **with_account(usage_api))
    assert usage_api.requests[0]["authorization"] == "Bearer env-tok"


def test_no_login_means_no_request(run, usage_api):
    usage_api.reply = usage_reply(fable=50)
    assert run(limits(u5=12, u7=41), now=AT, **with_account(usage_api)) == "5h 12% · 7d 41%"
    assert usage_api.requests == []


def test_the_account_fetch_can_be_turned_off(run, usage_api):
    login(run, AT)
    usage_api.reply = usage_reply(fable=50)
    out = run(limits(u5=12, u7=41), now=AT, **with_account(usage_api, USAGE_FORECAST_ACCOUNT="0"))
    assert out == "5h 12% · 7d 41%"
    assert usage_api.requests == []


def test_no_request_before_the_session_has_subscription_limits(run, usage_api):
    login(run, AT)
    usage_api.reply = usage_reply(fable=50)
    assert run({"model": {"id": "claude-fable-5-1"}}, now=AT, **with_account(usage_api)) == ""
    assert usage_api.requests == []


def test_a_gateway_session_neither_fetches_nor_shows_login_limits(run, usage_api):
    # A fresh fetch from another session is on disk, but this session is billed
    # through a gateway: only its spend limit applies.
    login(run, AT)
    run.cache.mkdir(parents=True)
    (run.cache / "account.tsv").write_text(f"#\t{AT}\nFable\t50\t{R7}\t{WEEK}\n")
    payload = limits(spend=62, spend_reset=AT + 10 * DAY)
    assert run(payload, now=AT, **with_account(usage_api)) == "spend 62%"
    assert usage_api.requests == []


def test_the_endpoint_is_asked_at_most_every_five_minutes(run, usage_api):
    login(run, AT, expires_in=DAY)
    usage_api.reply = usage_reply(fable=50)
    for offset in (0, 60, 299):
        run(limits(u5=12, u7=41), now=AT + offset, **with_account(usage_api))
    assert len(usage_api.requests) == 1
    run(limits(u5=12, u7=41), now=AT + 300, **with_account(usage_api))
    assert len(usage_api.requests) == 2


def test_a_failed_fetch_keeps_the_last_limits_until_they_are_stale(run, usage_api):
    login(run, AT, expires_in=DAY)
    usage_api.reply = usage_reply(fable=50)
    assert run(limits(u5=12, u7=41), now=AT, **with_account(usage_api)).endswith("Fable 50%")
    usage_api.reply = (401, '{"error": "expired"}')
    assert run(limits(u5=12, u7=41), now=AT + 600, **with_account(usage_api)).endswith("Fable 50%")
    assert run(limits(u5=12, u7=41), now=AT + 1500, **with_account(usage_api)) == "5h 12% · 7d 41%"
    assert len(usage_api.requests) == 3


@pytest.mark.parametrize("body", ["not json", "[]", '{"limits": "none"}', '{"limits": [null, 7]}'])
def test_a_garbage_reply_is_ignored(run, usage_api, body):
    login(run, AT)
    usage_api.reply = (200, body)
    assert run(limits(u5=12, u7=41), now=AT, **with_account(usage_api)) == "5h 12% · 7d 41%"


def test_a_scoped_limit_needs_a_name_and_a_reset(run, usage_api):
    login(run, AT)
    nameless = {"kind": "weekly_scoped", "group": "weekly", "percent": 90, "resets_at": endpoint_time(R7),
                "scope": {"model": {"id": None, "display_name": None}, "surface": None}}
    no_reset = {"kind": "weekly_scoped", "group": "weekly", "percent": 90, "resets_at": None,
                "scope": {"model": {"display_name": "Sonnet"}}}
    usage_api.reply = usage_reply(fable=50, extra=[nameless, no_reset])
    assert run(limits(u5=12, u7=41), now=AT, **with_account(usage_api)) == "5h 12% · 7d 41% · Fable 50%"


def test_other_scoped_limits_are_shown_by_their_name(run, usage_api):
    login(run, AT)
    sonnet = {"kind": "weekly_scoped", "group": "weekly", "percent": 20, "resets_at": endpoint_time(R7),
              "scope": {"model": {"id": None, "display_name": "Sonnet"}, "surface": None}}
    usage_api.reply = usage_reply(fable=50, extra=[sonnet])
    assert run(limits(u5=12, u7=41), now=AT, **with_account(usage_api)) == "5h 12% · 7d 41% · Fable 50% · Sonnet 20%"


def test_when_only_fable_runs_out_the_top_spender_is_ranked_by_fable(run, usage_api):
    # A spent $20 on Opus 5.5, B $5 on Fable 5.1: by Fable spending, B is the whole of it.
    login(run, AT)
    usage_api.reply = usage_reply(fable=78)
    transcript(run, SID_A, [title("a"), call(AT - 60, "m1", out=1_000_000)])
    transcript(run, SID_B, [title("b"), call(AT - 60, "m2", model="claude-fable-5-1", out=100_000)])
    out = run({**limits(u5=12, u7=41), "session_id": SID_A}, now=AT, **with_account(usage_api))
    assert out == "5h 12% · 7d 41% · " + fable_red(78) + " · top Fable: b 100%"


def test_when_the_five_hour_limit_runs_out_too_all_spending_counts(run, usage_api):
    login(run, AT)
    usage_api.reply = usage_reply(fable=78)
    transcript(run, SID_A, [title("a"), call(AT - 60, "m1", out=1_000_000)])
    transcript(run, SID_B, [title("b"), call(AT - 60, "m2", model="claude-fable-5-1", out=100_000)])
    out = run({**limits(u5=90, u7=41), "session_id": SID_B}, now=AT, **with_account(usage_api))
    assert out.endswith(" · top: a 80%")
