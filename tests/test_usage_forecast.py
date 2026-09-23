"""Tests for usage_forecast.sh, driven through its real stdin/stdout interface."""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
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

    def __call__(self, payload, now, stdin=None, **env):
        full_env = {**os.environ, "NO_COLOR": "1", "TZ": "UTC", "USAGE_FORECAST_NOW": str(now),
                    "USAGE_FORECAST_CACHE_DIR": str(self.cache), "USAGE_FORECAST_PROJECTS": str(self.projects),
                    "USAGE_FORECAST_SYNC": "1", **env}
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
    assert "\033[38;5;196m52% 3.9× cap 15:36" in run(limits(u5=52), now=S5 + 40 * 60, NO_COLOR=None)


def test_average_pace_over_one_always_runs_out_first(run):
    # 45% in 2 hours is 1.1x: 112% by the reset, so the cap comes first (18:46).
    assert run(limits(u5=45), now=S5 + 2 * HOUR) == "5h 45% 1.1× cap 18:46 (resets 19:20)"


def test_burning_fast_from_a_low_base_is_yellow_without_a_cap(run):
    # The last half hour went 8% -> 20%: 1.2x. At that rate the other 80% takes
    # 3.3 hours, and the window resets in 2.
    now = S5 + 3 * HOUR
    seed(run, (S5 + HOUR, 8, R5, None, None), (now - 5 * 60, 20, R5, None, None))
    assert run(limits(u5=20), now=now) == "5h 20% 1.2×"
    assert "\033[38;5;220m20% 1.2×" in run(limits(u5=20), now=now, NO_COLOR=None)


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
    (run.cache / "fleet.tsv").write_text(f"#\t{NOW - 3600}\t1800\n{SID_B}\t60\tb\n{SID_A}\t20\ta\n")
    out = run({**limits(u5=52), "session_id": SID_A}, now=NOW, USAGE_FORECAST_TOP="warn",
              USAGE_FORECAST_PROJECTS=str(run.projects / "missing"))
    assert out == RED
