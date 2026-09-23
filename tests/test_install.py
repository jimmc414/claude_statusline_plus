"""Tests for install.sh, run against a throwaway Claude Code config directory."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "install.sh"
SCRIPTS = ["cache_warm.sh", "usage_forecast.sh", "statusline_plus.sh"]


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "claude config"  # the space is deliberate: macOS home dirs can have one
    d.mkdir()
    return d


def run_installer(cfg, *args, check=True, cwd=None):
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg)}
    proc = subprocess.run(["bash", str(INSTALLER), *args], text=True, capture_output=True,
                          env=env, timeout=60, cwd=cwd)
    if check:
        assert proc.returncode == 0, proc.stderr
    return proc


def settings(cfg):
    return json.loads((cfg / "settings.json").read_text())


def expected_command(cfg):
    return f'bash "{cfg / "statusline_plus.sh"}"'


def old_command(cfg):
    """What the installer configured before usage_forecast.sh existed."""
    return f'bash "{cfg / "cache_warm.sh"}"'


def test_fresh_install_creates_scripts_and_settings(cfg):
    run_installer(cfg)
    for name in SCRIPTS:
        script = cfg / name
        assert script.read_text() == (ROOT / name).read_text()
        assert os.access(script, os.X_OK)
    assert settings(cfg) == {"statusLine": {"type": "command", "command": expected_command(cfg),
                                            "refreshInterval": 30}}


def run_status_line(cfg, payload, tmp_path):
    env = {**os.environ, "NO_COLOR": "1", "USAGE_FORECAST_CACHE_DIR": str(tmp_path / "usage-cache"),
           "USAGE_FORECAST_TOP": "never", "USAGE_FORECAST_ACCOUNT": "0", "CLAUDE_CONFIG_DIR": str(cfg)}
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    proc = subprocess.run(["bash", "-c", settings(cfg)["statusLine"]["command"]], text=True,
                          input=json.dumps(payload), capture_output=True, timeout=20, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    return proc.stdout.strip()


def test_configured_command_actually_runs(cfg, tmp_path):
    run_installer(cfg)
    payload = {"prompt_cache": {"caching_observed": True, "ttl": "1h", "expires_at": 4_000_000_000}}
    assert run_status_line(cfg, payload, tmp_path).startswith("cache warm ")


def test_configured_command_joins_both_segments(cfg, tmp_path):
    run_installer(cfg)
    now = int(time.time())
    payload = {"prompt_cache": {"caching_observed": True, "ttl": "1h", "expires_at": now + 1860},
               "rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": now + 4 * 3600 + 30}}}
    assert run_status_line(cfg, payload, tmp_path) == "cache warm 31m · 5h 12% (resets in 4h00m)"


def test_configured_command_prints_nothing_before_the_first_response(cfg, tmp_path):
    run_installer(cfg)
    assert run_status_line(cfg, {}, tmp_path) == ""


def test_existing_settings_keep_their_other_keys(cfg):
    (cfg / "settings.json").write_text(json.dumps({"model": "opus", "env": {"A": "1"}}))
    run_installer(cfg)
    s = settings(cfg)
    assert s["model"] == "opus" and s["env"] == {"A": "1"}
    assert s["statusLine"]["command"] == expected_command(cfg)
    assert json.loads((cfg / "settings.json.bak-statusline-plus").read_text()) == {"model": "opus", "env": {"A": "1"}}


def test_custom_status_line_is_never_modified(cfg):
    original = json.dumps({"statusLine": {"type": "command", "command": "npx my-statusline"}}, indent=2)
    (cfg / "settings.json").write_text(original)
    proc = run_installer(cfg)
    assert (cfg / "settings.json").read_text() == original
    assert not (cfg / "settings.json.bak-statusline-plus").exists()
    assert all((cfg / name).exists() for name in SCRIPTS)
    assert "npx my-statusline" in proc.stdout and "left untouched" in proc.stdout
    assert "cache_warm.sh" in proc.stdout and "usage_forecast.sh" in proc.stdout


def test_reinstall_is_idempotent(cfg):
    run_installer(cfg)
    before = (cfg / "settings.json").read_text()
    proc = run_installer(cfg)
    assert (cfg / "settings.json").read_text() == before
    assert "already configured" in proc.stdout


def test_adds_missing_refresh_interval_to_its_own_status_line(cfg):
    (cfg / "settings.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": expected_command(cfg)}}))
    run_installer(cfg)
    assert settings(cfg)["statusLine"]["refreshInterval"] == 30


def test_upgrades_an_install_that_ran_cache_warm_alone(cfg):
    (cfg / "settings.json").write_text(json.dumps(
        {"model": "opus", "statusLine": {"type": "command", "command": old_command(cfg), "refreshInterval": 10}}))
    proc = run_installer(cfg)
    assert settings(cfg) == {"model": "opus", "statusLine": {"type": "command", "command": expected_command(cfg),
                                                             "refreshInterval": 10}}
    assert "Upgraded" in proc.stdout


def test_upgrade_adds_a_missing_refresh_interval(cfg):
    (cfg / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": old_command(cfg)}}))
    run_installer(cfg)
    assert settings(cfg)["statusLine"] == {"type": "command", "command": expected_command(cfg), "refreshInterval": 30}


def test_keeps_a_refresh_interval_the_user_chose(cfg):
    (cfg / "settings.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": expected_command(cfg), "refreshInterval": 5}}))
    run_installer(cfg)
    assert settings(cfg)["statusLine"]["refreshInterval"] == 5


@pytest.mark.parametrize("content", ["{ not json", "[1, 2]", ""])
def test_unusable_settings_file_is_left_alone(cfg, content):
    (cfg / "settings.json").write_text(content)
    proc = run_installer(cfg)
    assert (cfg / "settings.json").read_text() == content
    assert "not valid JSON" in proc.stderr
    assert all((cfg / name).exists() for name in SCRIPTS)


def test_symlinked_settings_stay_a_symlink(cfg, tmp_path):
    real = tmp_path / "dotfiles-settings.json"
    real.write_text(json.dumps({"theme": "dark"}))
    (cfg / "settings.json").symlink_to(real)
    run_installer(cfg)
    assert (cfg / "settings.json").is_symlink()
    assert json.loads(real.read_text())["statusLine"]["command"] == expected_command(cfg)
    assert json.loads(real.read_text())["theme"] == "dark"


def test_uninstall_removes_what_install_added(cfg):
    (cfg / "settings.json").write_text(json.dumps({"model": "opus"}))
    run_installer(cfg)
    run_installer(cfg, "--uninstall")
    assert not any((cfg / name).exists() for name in SCRIPTS)
    assert settings(cfg) == {"model": "opus"}


def test_uninstall_removes_an_old_cache_warm_status_line(cfg):
    (cfg / "settings.json").write_text(json.dumps({"model": "opus", "statusLine": {"command": old_command(cfg)}}))
    run_installer(cfg, "--uninstall")
    assert settings(cfg) == {"model": "opus"}


def test_uninstall_leaves_a_custom_status_line_alone(cfg):
    original = json.dumps({"statusLine": {"type": "command", "command": "bash ~/mine.sh"}})
    (cfg / "settings.json").write_text(original)
    run_installer(cfg)
    proc = run_installer(cfg, "--uninstall")
    assert (cfg / "settings.json").read_text() == original
    assert "left untouched" in proc.stdout


def test_uninstall_when_nothing_is_installed(cfg):
    run_installer(cfg, "--uninstall")


def test_unknown_option_fails(cfg):
    proc = run_installer(cfg, "--frobnicate", check=False)
    assert proc.returncode != 0 and "unknown option" in proc.stderr
    assert not any((cfg / name).exists() for name in SCRIPTS)


def test_help_prints_usage(cfg):
    assert "--uninstall" in run_installer(cfg, "--help").stdout


# --- project scope -----------------------------------------------------------

@pytest.fixture
def proj(tmp_path):
    d = tmp_path / "my project"
    d.mkdir()
    return d


def project_command(proj):
    return f'bash "{proj / ".claude" / "statusline_plus.sh"}"'


def local_settings(proj):
    return json.loads((proj / ".claude" / "settings.local.json").read_text())


def test_project_install_touches_only_the_project(cfg, proj):
    proc = run_installer(cfg, "--project", str(proj))
    assert all(os.access(proj / ".claude" / name, os.X_OK) for name in SCRIPTS)
    assert local_settings(proj) == {"statusLine": {"type": "command", "command": project_command(proj),
                                                   "refreshInterval": 30}}
    assert list(cfg.iterdir()) == []  # nothing global was created
    assert str(proj) in proc.stdout


def test_project_defaults_to_the_current_directory(cfg, proj):
    run_installer(cfg, "--project", cwd=proj)
    assert local_settings(proj)["statusLine"]["command"] == project_command(proj)


def test_project_install_keeps_other_local_settings(cfg, proj):
    (proj / ".claude").mkdir()
    (proj / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": {"allow": ["Read"]}}))
    run_installer(cfg, "--project", str(proj))
    assert local_settings(proj)["permissions"] == {"allow": ["Read"]}
    assert local_settings(proj)["statusLine"]["command"] == project_command(proj)


def test_project_install_will_not_shadow_a_global_custom_status_line(cfg, proj):
    (cfg / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "bash ~/mine.sh"}}))
    proc = run_installer(cfg, "--project", str(proj))
    assert not (proj / ".claude" / "settings.local.json").exists()
    assert "bash ~/mine.sh" in proc.stdout and "left untouched" in proc.stdout
    assert all((proj / ".claude" / name).exists() for name in SCRIPTS)  # still installed, for the manual snippet


def test_project_install_will_not_shadow_the_projects_shared_status_line(cfg, proj):
    (proj / ".claude").mkdir()
    (proj / ".claude" / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "npx team-statusline"}}))
    proc = run_installer(cfg, "--project", str(proj))
    assert not (proj / ".claude" / "settings.local.json").exists()
    assert "npx team-statusline" in proc.stdout


def test_project_install_on_top_of_a_global_install_of_this_tool(cfg, proj):
    run_installer(cfg)
    run_installer(cfg, "--project", str(proj))
    assert local_settings(proj)["statusLine"]["command"] == project_command(proj)


@pytest.mark.parametrize("flag_first", [True, False])
def test_project_uninstall_leaves_the_global_install_alone(cfg, proj, flag_first):
    run_installer(cfg)
    run_installer(cfg, "--project", str(proj))
    scope = ["--project", str(proj)]
    run_installer(cfg, *(["--uninstall"] + scope if flag_first else scope + ["--uninstall"]))
    assert not any((proj / ".claude" / name).exists() for name in SCRIPTS)
    assert "statusLine" not in local_settings(proj)
    assert all((cfg / name).exists() for name in SCRIPTS)
    assert settings(cfg)["statusLine"]["command"] == expected_command(cfg)


def test_project_directory_must_exist(cfg, tmp_path):
    proc = run_installer(cfg, "--project", str(tmp_path / "nope"), check=False)
    assert proc.returncode != 0 and "not a directory" in proc.stderr


def test_global_flag_is_the_default(cfg):
    run_installer(cfg, "--global")
    assert settings(cfg)["statusLine"]["command"] == expected_command(cfg)


def test_project_install_on_top_of_an_old_global_install(cfg, proj):
    (cfg / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": old_command(cfg)}}))
    run_installer(cfg, "--project", str(proj))
    assert local_settings(proj)["statusLine"]["command"] == project_command(proj)
