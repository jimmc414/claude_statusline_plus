"""Tests for cache_warm.sh, driven through its real stdin/stdout interface."""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "cache_warm.sh"
T0 = 1_790_000_000  # arbitrary fixed epoch; all entries are placed relative to it
MODEL = "claude-fable-5-1"
HOUR = 3600


def iso(epoch, millis=True):
    fmt = "%Y-%m-%dT%H:%M:%S.123Z" if millis else "%Y-%m-%dT%H:%M:%SZ"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(fmt)


def user(at, **extra):
    return {"type": "user", "timestamp": iso(at), "isSidechain": False,
            "message": {"role": "user", "content": "hi"}, **extra}


def call(at, msg_id="msg_1", h1=1000, m5=0, read=49_000, inp=2, model=MODEL, tiers=True, **extra):
    usage: dict = {"input_tokens": inp, "cache_read_input_tokens": read,
                   "cache_creation_input_tokens": h1 + m5, "output_tokens": 10}
    if tiers:
        usage["cache_creation"] = {"ephemeral_1h_input_tokens": h1, "ephemeral_5m_input_tokens": m5}
    return {"type": "assistant", "timestamp": iso(at), "isSidechain": False,
            "message": {"id": msg_id, "model": model, "role": "assistant", "usage": usage,
                        "content": [{"type": "text", "text": "ok"}]}, **extra}


def prompt_cache(expires_at, ttl="1h", recache: int | None = 80_000, observed=True, **extra):
    """The .prompt_cache object Claude Code >= 2.1.251 puts in the payload."""
    return {"warm": True, "caching_observed": observed, "ttl": ttl, "expires_at": expires_at,
            "requests": 5, "misses": 0, "hit_ratio": 0.9, "recache_tokens_if_cold": recache, **extra}


@pytest.fixture
def run(tmp_path):
    def _run(entries, now, model_id=MODEL, pc=None, raw_tail="", payload=None, stdin=None, **env):
        transcript = tmp_path / "session.jsonl"
        transcript.write_text("".join(json.dumps(e) + "\n" for e in entries) + raw_tail)
        if payload is None:
            payload = {"transcript_path": str(transcript), "model": {"id": model_id}}
            if pc is not None:
                payload["prompt_cache"] = pc
        full_env = {**os.environ, "NO_COLOR": "1", "CACHE_WARM_NOW": str(now), **env}
        for key in [k for k, v in full_env.items() if v is None]:
            del full_env[key]
        proc = subprocess.run(["bash", str(SCRIPT)], text=True, capture_output=True, env=full_env,
                              input=json.dumps(payload) if stdin is None else stdin, timeout=20)
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr == ""
        return proc.stdout.strip()
    return _run


# =============================================================================
# Transcript path (older Claude Code: no .prompt_cache in the payload)
# =============================================================================

# --- states -----------------------------------------------------------------

def test_warm_one_hour_tier(run):
    assert run([user(T0), call(T0 + 5)], now=T0 + 29 * 60) == "cache warm 31m"


def test_long_style_matches_desktop_wording(run):
    out = run([user(T0), call(T0 + 5)], now=T0 + 29 * 60, CACHE_WARM_STYLE="long")
    assert out == "Prompt cache warm, about 31 min left."


def test_expiring_is_yellow_and_warm_is_green(run):
    entries = [user(T0), call(T0 + 5)]
    assert "\033[38;5;220m" in run(entries, now=T0 + 56 * 60, NO_COLOR=None)
    assert "\033[38;5;40m" in run(entries, now=T0 + 29 * 60, NO_COLOR=None)
    assert run(entries, now=T0 + 56 * 60) == "cache warm 4m"


def test_cold_reports_tokens_to_recache(run):
    out = run([user(T0), call(T0 + 5, h1=1000, read=49_000, inp=2)], now=T0 + 61 * 60)
    assert out == "cache cold · 50k"


def test_cold_exactly_at_expiry(run):
    assert run([user(T0), call(T0 + 5)], now=T0 + HOUR).startswith("cache cold")
    assert run([user(T0), call(T0 + 5)], now=T0 + HOUR - 1) == "cache warm <1m"


def test_long_cold_wording(run):
    out = run([user(T0), call(T0 + 5)], now=T0 + 2 * HOUR, CACHE_WARM_STYLE="long")
    assert out == "Prompt cache cold; next message re-caches ~50k tokens."


def test_under_a_minute(run):
    entries = [user(T0), call(T0 + 5)]
    assert run(entries, now=T0 + HOUR - 45) == "cache warm <1m"
    out = run(entries, now=T0 + HOUR - 45, CACHE_WARM_STYLE="long")
    assert out == "Prompt cache expiring, under a minute left."


# --- TTL tier ---------------------------------------------------------------

def test_five_minute_tier_is_labelled(run):
    entries = [user(T0), call(T0 + 1, h1=0, m5=1000)]
    assert run(entries, now=T0 + 120) == "cache warm 3m (5m ttl)"
    assert run(entries, now=T0 + 120, CACHE_WARM_STYLE="long") == \
        "Prompt cache warm, about 3 min left (5 min TTL)."
    assert run(entries, now=T0 + 360).startswith("cache cold")


def test_mixed_tiers_use_the_shorter(run):
    assert run([user(T0), call(T0 + 1, h1=5000, m5=1000)], now=T0 + 360).startswith("cache cold")


def test_tier_looks_back_when_last_call_created_nothing(run):
    entries = [user(T0 - 100), call(T0 - 90, "msg_a", h1=0, m5=1000),
               user(T0), call(T0 + 1, "msg_b", h1=0, m5=0)]
    assert run(entries, now=T0 + 360).startswith("cache cold")


def test_latest_tier_wins_over_older(run):
    entries = [user(T0 - 100), call(T0 - 90, "msg_a", h1=1000, m5=0),
               user(T0), call(T0 + 1, "msg_b", h1=0, m5=1000)]
    assert run(entries, now=T0 + 360).startswith("cache cold")


def test_default_ttl_when_tier_unknown(run):
    entries = [user(T0), call(T0 + 1, tiers=False)]
    assert run(entries, now=T0 + 360) == "cache warm 54m"
    assert run(entries, now=T0 + 360, CACHE_WARM_DEFAULT_TTL="300").startswith("cache cold")


def test_garbage_env_values_fall_back_to_defaults(run):
    out = run([user(T0), call(T0 + 1, tiers=False)], now=T0 + 360,
              CACHE_WARM_DEFAULT_TTL="abc", CACHE_WARM_TAIL="-5")
    assert out == "cache warm 54m"


# --- which moment counts as the cache touch -----------------------------------

def test_anchors_on_request_start_not_first_streamed_block(run):
    # Two minutes of thinking before the first block is written.
    entries = [user(T0), call(T0 + 120)]
    assert run(entries, now=T0 + 58 * 60) == "cache warm 2m"


def test_multi_block_message_uses_request_start(run):
    entries = [user(T0), call(T0 + 10), call(T0 + 11), call(T0 + 300)]
    assert run(entries, now=T0 + 29 * 60) == "cache warm 31m"


def test_falls_back_to_first_block_without_preceding_user_entry(run):
    entries = [call(T0 + 10), call(T0 + 300)]
    assert run(entries, now=T0 + 10 + 29 * 60) == "cache warm 31m"


def test_tool_loop_uses_latest_request(run):
    entries = [user(T0), call(T0 + 5, "msg_a"),
               user(T0 + 600), call(T0 + 605, "msg_b")]
    assert run(entries, now=T0 + 600 + 29 * 60) == "cache warm 31m"


def test_timestamps_without_fractional_seconds(run):
    entries = [user(T0), call(T0 + 5)]
    for e in entries:
        e["timestamp"] = iso(T0, millis=False)
    assert run(entries, now=T0 + 29 * 60) == "cache warm 31m"


def test_unparseable_timestamp_is_skipped_not_fatal(run):
    bad = call(T0 + 4000, "msg_bad")
    bad["timestamp"] = "yesterday-ish"
    assert run([user(T0), call(T0 + 5), bad], now=T0 + 29 * 60) == "cache warm 31m"


# --- entries that must not count --------------------------------------------------

def test_sidechain_calls_do_not_refresh_main_cache(run):
    entries = [user(T0), call(T0 + 5),
               user(T0 + 3000, isSidechain=True), call(T0 + 3005, "msg_side", isSidechain=True)]
    assert run(entries, now=T0 + 61 * 60).startswith("cache cold")


def test_synthetic_and_api_error_entries_ignored(run):
    entries = [user(T0), call(T0 + 5),
               user(T0 + 3000), call(T0 + 3001, "msg_err", isApiErrorMessage=True),
               user(T0 + 3100), call(T0 + 3101, "msg_syn", model="<synthetic>")]
    assert run(entries, now=T0 + 61 * 60).startswith("cache cold")


def test_call_with_no_cache_tokens_is_not_a_touch(run):
    entries = [user(T0), call(T0 + 5),
               user(T0 + 3000), call(T0 + 3001, "msg_nocache", h1=0, m5=0, read=0)]
    assert run(entries, now=T0 + 61 * 60).startswith("cache cold")


def test_partially_written_last_line_is_skipped(run):
    out = run([user(T0), call(T0 + 5)], now=T0 + 29 * 60, raw_tail='{"type":"assistant","timest')
    assert out == "cache warm 31m"


def test_full_scan_when_tail_window_has_no_calls(run):
    filler = [{"type": "attachment", "timestamp": iso(T0 + 10)} for _ in range(50)]
    out = run([user(T0), call(T0 + 5)] + filler, now=T0 + 29 * 60, CACHE_WARM_TAIL="10")
    assert out == "cache warm 31m"


# --- model switch ---------------------------------------------------------------

def test_model_switch_is_cold(run):
    out = run([user(T0), call(T0 + 5)], now=T0 + 60, model_id="claude-opus-5")
    assert out == "cache cold (model changed) · 50k"


@pytest.mark.parametrize("payload_id,transcript_id", [
    ("claude-fable-5-1[1m]", "claude-fable-5-1"),
    ("claude-haiku-4-5", "claude-haiku-4-5-20251001"),
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
    ("", "claude-fable-5-1"),
])
def test_equivalent_model_ids_stay_warm(run, payload_id, transcript_id):
    out = run([user(T0), call(T0 + 5, model=transcript_id)], now=T0 + 60, model_id=payload_id)
    assert out == "cache warm 59m"


# --- nothing to show ------------------------------------------------------------

def test_no_api_calls_prints_nothing(run):
    assert run([user(T0)], now=T0 + 60) == ""


def test_missing_transcript_prints_nothing(run):
    assert run([], now=T0, payload={"transcript_path": "/nonexistent/x.jsonl"}) == ""


def test_payload_without_transcript_path_prints_nothing(run):
    assert run([], now=T0, payload={}) == ""


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", "null"])
def test_garbage_stdin_prints_nothing(run, stdin):
    assert run([], now=T0, stdin=stdin) == ""


def test_model_given_as_string_is_tolerated(run, tmp_path):
    entries = [user(T0), call(T0 + 5)]
    (tmp_path / "session.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    # run() rewrites the same transcript file, so the path below stays valid.
    payload = {"transcript_path": str(tmp_path / "session.jsonl"), "model": "claude-fable-5-1"}
    assert run(entries, now=T0 + 29 * 60, payload=payload) == "cache warm 31m"


# =============================================================================
# Payload path (.prompt_cache from Claude Code >= 2.1.251)
# =============================================================================

def test_payload_expiry_works_without_any_transcript(run):
    payload = {"model": {"id": MODEL}, "prompt_cache": prompt_cache(T0 + HOUR)}
    assert run([], now=T0 + 29 * 60, payload=payload) == "cache warm 31m"


def test_payload_cold_uses_cli_recache_estimate(run):
    out = run([user(T0), call(T0 + 5)], now=T0 + 2 * HOUR, pc=prompt_cache(T0 + HOUR, recache=123_456))
    assert out == "cache cold · 123k"


def test_payload_expiry_is_trusted_over_an_earlier_transcript_anchor(run):
    # The CLI stamps expires_at at request dispatch and also sees cache touches the
    # transcript can't (forked agents), so a slightly later payload expiry is real.
    entries = [user(T0), call(T0 + 150)]
    out = run(entries, now=T0 + 58 * 60, pc=prompt_cache(T0 + 200 + HOUR))
    assert out == "cache warm 5m"


@pytest.mark.parametrize("gap", [100, 254, 599, 601])
def test_no_cliff_when_a_forked_agent_touches_the_cache_soon_after(run, gap):
    entries = [user(T0), call(T0 + 10)]
    out = run(entries, now=T0 + HOUR + 1, pc=prompt_cache(T0 + gap + HOUR))
    assert out.startswith("cache warm")


def test_payload_wins_when_it_saw_a_touch_the_transcript_did_not(run):
    # e.g. a forked agent read the main cache 30 minutes after the last main call.
    entries = [user(T0), call(T0 + 5)]
    out = run(entries, now=T0 + 61 * 60, pc=prompt_cache(T0 + 1800 + HOUR))
    assert out == "cache warm 29m"


def test_transcript_never_extends_the_payload_expiry(run):
    entries = [user(T0 + 300), call(T0 + 305)]
    assert run(entries, now=T0 + 29 * 60, pc=prompt_cache(T0 + HOUR)) == "cache warm 31m"


def test_payload_five_minute_ttl_is_labelled_and_overrides_transcript_tier(run):
    entries = [user(T0), call(T0 + 1, h1=1000)]  # transcript says 1h; CLI says 5m now
    out = run(entries, now=T0 + 120, pc=prompt_cache(T0 + 300, ttl="5m"))
    assert out == "cache warm 3m (5m ttl)"


def test_payload_null_expiry_is_cold(run):
    out = run([user(T0), call(T0 + 5)], now=T0 + 60, pc=prompt_cache(None, recache=70_000))
    assert out == "cache cold · 70k"


def test_payload_caching_never_observed_is_off(run):
    pc = prompt_cache(None, observed=False, recache=0)
    assert run([], now=T0 + 60, pc=pc) == "cache off"
    assert run([], now=T0 + 60, pc=pc, CACHE_WARM_STYLE="long") == "Prompt caching not reported by the API."


def test_payload_cold_without_any_token_estimate(run):
    assert run([], now=T0 + 2 * HOUR, payload={"prompt_cache": prompt_cache(T0 + HOUR, recache=None)}) \
        == "cache cold"


def test_model_switch_overrides_a_warm_payload(run):
    out = run([user(T0), call(T0 + 5)], now=T0 + 60, model_id="claude-opus-5",
              pc=prompt_cache(T0 + HOUR, recache=90_000))
    assert out == "cache cold (model changed) · 90k"


@pytest.mark.parametrize("bad", ["warm", 42, [], True])
def test_malformed_prompt_cache_falls_back_to_transcript(run, bad):
    assert run([user(T0), call(T0 + 5)], now=T0 + 29 * 60, pc=bad) == "cache warm 31m"


def test_payload_fractional_expiry_and_unknown_ttl(run):
    pc = prompt_cache(T0 + HOUR + 0.7, ttl="2h")
    assert run([], now=T0 + 29 * 60, payload={"prompt_cache": pc}) == "cache warm 31m"


# =============================================================================
# Regressions: each of these pins a mutant that once survived the suite
# =============================================================================

def test_synthetic_and_api_error_ignored_with_no_model_in_payload(run, tmp_path):
    # No .model in the payload, so the model-change check cannot mask the filter.
    entries = [user(T0), call(T0 + 5),
               user(T0 + 3000), call(T0 + 3001, "msg_err", isApiErrorMessage=True),
               user(T0 + 3100), call(T0 + 3101, "msg_syn", model="<synthetic>")]
    payload = {"transcript_path": str(tmp_path / "session.jsonl")}
    assert run(entries, now=T0 + 61 * 60, payload=payload) == "cache cold · 50k"


def test_first_call_of_a_session_is_a_pure_cache_write(run):
    entries = [user(T0), call(T0 + 5, h1=47_000, m5=0, read=0)]
    assert run(entries, now=T0 + 29 * 60) == "cache warm 31m"
    assert run(entries, now=T0 + 61 * 60) == "cache cold · 47k"


def test_anchor_cut_off_by_the_tail_window_is_recovered(run):
    # One message with more content blocks than the window holds: the user entry
    # that triggered it is outside `tail -n`, so the wider scan must find it.
    entries = [user(T0)] + [call(T0 + 200 + i) for i in range(120)]
    assert run(entries, now=T0 + HOUR + 50).startswith("cache cold")
    assert run(entries, now=T0 + HOUR - 120) == "cache warm 2m"


def test_back_to_back_calls_anchor_on_the_newer_request(run):
    # The entry before the last call is itself a call: do not borrow its time.
    entries = [user(T0), call(T0 + 5, "msg_a"), call(T0 + 2000, "msg_b")]
    assert run(entries, now=T0 + 4000) == "cache warm 26m"


def test_later_stamped_out_of_band_entry_cannot_extend_the_expiry(run):
    entries = [user(T0), user(T0 + 86_400), call(T0 + 15)]
    assert run(entries, now=T0 + HOUR + 60).startswith("cache cold")


def test_remaining_never_exceeds_one_ttl(run):
    payload = {"prompt_cache": prompt_cache(T0 + 10 * HOUR)}
    assert run([], now=T0, payload=payload) == "cache warm 60m"


def test_warn_window_boundary_turns_yellow(run):
    entries = [user(T0), call(T0 + 5)]  # 1h tier: expiry T0+3600, warn window 600s
    assert "\033[38;5;40m" in run(entries, now=T0 + HOUR - 601, NO_COLOR=None)
    assert "\033[38;5;220m" in run(entries, now=T0 + HOUR - 600, NO_COLOR=None)
    assert "\033[38;5;220m" in run(entries, now=T0 + HOUR - 599, NO_COLOR=None)


def test_five_minute_warn_window_has_a_sixty_second_floor(run):
    entries = [user(T0), call(T0 + 1, h1=0, m5=1000)]  # ttl/6 = 50, floored to 60
    assert "\033[38;5;40m" in run(entries, now=T0 + 300 - 61, NO_COLOR=None)
    assert "\033[38;5;220m" in run(entries, now=T0 + 300 - 60, NO_COLOR=None)
    assert "\033[38;5;220m" in run(entries, now=T0 + 300 - 51, NO_COLOR=None)


def test_five_minute_label_only_for_a_real_five_minute_ttl(run):
    out = run([user(T0), call(T0 + 1, tiers=False)], now=T0 + 10, CACHE_WARM_DEFAULT_TTL="120")
    assert out == "cache warm 1m"


def test_garbage_now_falls_back_to_the_real_clock(run):
    past = int(time.time()) - 2 * HOUR
    entries = [user(past), call(past + 5)]
    assert run(entries, now=T0, CACHE_WARM_NOW="abc").startswith("cache cold")


# --- model ids ---------------------------------------------------------------

@pytest.mark.parametrize("payload_id,transcript_id", [
    ("claude-fable-5", "claude-fable-5-1"),       # one id is a prefix of the other
    ("claude-fable-5-1", "claude-fable-5"),
    ("claude-fable-5[1m]", "claude-fable-5-1"),
    ("claude-opus-5-20260801", "claude-opus-5-1-20260801"),
])
def test_prefix_related_ids_are_still_different_models(run, payload_id, transcript_id):
    out = run([user(T0), call(T0 + 5, model=transcript_id)], now=T0 + 60, model_id=payload_id)
    assert out == "cache cold (model changed) · 50k"


@pytest.mark.parametrize("payload_id", ["opus", "[1m]", "us.anthropic.claude-opus-5-v1:0",
                                         "arn:aws:bedrock:us-east-1:1:inference-profile/x"])
def test_unfamiliar_id_formats_never_read_as_a_model_change(run, payload_id):
    out = run([user(T0), call(T0 + 5, model="claude-opus-5")], now=T0 + 60, model_id=payload_id)
    assert out == "cache warm 59m"


# --- hostile or malformed input ---------------------------------------------------

def test_transcript_path_with_a_backslash(run, tmp_path):
    odd = tmp_path / "a\\b"
    odd.mkdir()
    transcript = odd / "s.jsonl"
    transcript.write_text("".join(json.dumps(e) + "\n" for e in [user(T0), call(T0 + 5)]))
    payload = {"transcript_path": str(transcript), "model": {"id": "claude-opus-5"}}
    assert run([], now=T0 + 60, payload=payload) == "cache cold (model changed) · 50k"


@pytest.mark.parametrize("poison", [
    '{"type":"assistant","timestamp":"2026-09-21T08:09:22.465Z","message":"oops"}',
    '{"type":"assistant","timestamp":"2026-09-21T08:09:22.465Z","message":{"usage":"oops"}}',
    '{"type":"assistant","timestamp":"2026-09-21T08:09:22.465Z","message":{"usage":{"cache_read_input_tokens":"9"}}}',
    "5", '"str"', "[1, 2]", "null",
])
def test_one_malformed_line_does_not_abort_the_scan(run, poison):
    out = run([user(T0), call(T0 + 5)], now=T0 + 29 * 60, raw_tail=poison + "\n")
    assert out == "cache warm 31m"


@pytest.mark.parametrize("expires_at", [1e17, 1.5e300, -5, 0])
def test_absurd_payload_expiry_falls_back_to_the_transcript(run, expires_at):
    out = run([user(T0), call(T0 + 5)], now=T0 + 29 * 60, pc=prompt_cache(expires_at))
    assert out == "cache warm 31m"


def test_absurd_recache_estimate_falls_back_to_context_size(run):
    out = run([user(T0), call(T0 + 5)], now=T0 + 2 * HOUR, pc=prompt_cache(T0 + HOUR, recache=1e30))
    assert out == "cache cold · 50k"


def test_calls_without_any_id_are_not_merged_into_one(run):
    older, newer = call(T0 + 5), call(T0 + 2005)
    for e in (older, newer):
        del e["message"]["id"]
    entries = [user(T0), older, user(T0 + 2000), newer]
    assert run(entries, now=T0 + 2000 + 29 * 60) == "cache warm 31m"


def test_unreadable_transcript_is_silent(run, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    locked = tmp_path / "locked.jsonl"
    locked.write_text(json.dumps(call(T0)) + "\n")
    locked.chmod(0)
    try:
        assert run([], now=T0 + 60, payload={"transcript_path": str(locked)}) == ""
    finally:
        locked.chmod(0o600)
