from __future__ import annotations

import json
import os
import plistlib
import stat
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

from pneu_packaging import setup as harness_setup


def _write_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def installation(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    prefix = home / ".pneu"
    home.mkdir()
    (prefix / "bin").mkdir(parents=True)
    (prefix / "skills" / "shared" / "pneu").mkdir(parents=True)
    (
        prefix
        / "current"
        / "share"
        / "pneu"
        / "integrations"
        / "hermes"
        / "pneu"
    ).mkdir(parents=True)
    for command in (
        "rt-wait-inbox",
        "rt-stop-gate",
        "rt-inbox",
        "rt-ack",
        "rt-say",
        "rt-codex-wake",
        "rt-codex-session-start",
    ):
        _write_executable(prefix / "bin" / command)
    _write_executable(
        home / ".npm-global" / "bin" / "codex",
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"--version\" ]; then\n"
        "  printf '%s\\n' 'codex-cli 0.144.6'\n"
        "fi\n"
        "exit 0\n",
    )
    return home, prefix


def _run(
    capsys: pytest.CaptureFixture[str],
    home: Path,
    prefix: Path,
    *arguments: str,
) -> tuple[int, dict]:
    result = harness_setup.main(
        [
            *arguments,
            "--home",
            str(home),
            "--prefix",
            str(prefix),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    return result, json.loads(captured.out)


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | str | None]]:
    snapshot: dict[str, tuple[str, bytes | str | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            snapshot[relative] = ("link", os.readlink(path))
        elif path.is_file():
            snapshot[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            snapshot[relative] = ("dir", None)
    return snapshot


def _all_harness_args() -> tuple[str, ...]:
    return (
        "--harness",
        "claude",
        "--harness",
        "hermes",
        "--harness",
        "codex",
    )


def test_default_plan_is_read_only(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    before = _tree_snapshot(home)

    code, result = _run(capsys, home, prefix, *_all_harness_args())

    assert code == 0
    assert result["command"] == "plan"
    assert result["writes"] is False
    assert result["launchctl_invoked"] is False
    assert set(result["harnesses"]) == {"claude", "hermes", "codex"}
    assert _tree_snapshot(home) == before
    assert not (prefix / ".runtime").exists()
    assert not (prefix / "harness-setup.json").exists()


def test_codex_setup_honors_one_static_codex_home_and_runtime_override(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation
    codex_home = home / "custom-codex-home"
    runtime = home / "custom-roundtable-runtime"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))

    code, result = _run(
        capsys,
        home,
        prefix,
        "apply",
        "--harness",
        "codex",
    )

    assert code == 0, result
    assert (codex_home / "hooks.json").is_file()
    assert (codex_home / "skills" / "pneu").is_symlink()
    assert not (home / ".codex").exists()
    assert runtime.is_dir()
    for label in harness_setup.CODEX_LABELS:
        plist_path = home / "Library" / "LaunchAgents" / f"{label}.plist"
        payload = plistlib.loads(plist_path.read_bytes())
        environment = payload["EnvironmentVariables"]
        assert environment["CODEX_HOME"] == str(codex_home)
        assert environment["RT_RUNTIME_DIR"] == str(runtime)
        assert environment["RT_CODEX_RUNTIME_DIR"] == str(runtime)
    app_payload = plistlib.loads(
        (
            home
            / "Library"
            / "LaunchAgents"
            / "com.roundtable.codex-app-server.plist"
        ).read_bytes()
    )
    assert app_payload["ProgramArguments"][-1] == (
        f"unix://{codex_home}/app-server-control/app-server-control.sock"
    )
    assert harness_setup._codex_reload_marker_path(prefix).is_file()


def test_clean_apply_status_idempotence_and_remove(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation

    code, result = _run(
        capsys, home, prefix, "apply", *_all_harness_args()
    )
    assert code == 0
    assert result["restart_required"] is True
    assert result["launchctl_invoked"] is False

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    groups = harness_setup._claude_groups(prefix)
    assert groups["SessionStart"] in settings["hooks"]["SessionStart"]
    assert groups["Stop"] in settings["hooks"]["Stop"]
    session_command = groups["SessionStart"]["hooks"][0]
    assert session_command["command"] == str(prefix / "bin" / "rt-wait-inbox")
    assert session_command["args"] == ["--claude-hook"]
    assert session_command["asyncRewake"] is True
    assert (
        session_command["timeout"]
        == harness_setup.CLAUDE_HOOK_TIMEOUT_SECONDS
    )
    stop_command = groups["Stop"]["hooks"][0]
    assert stop_command["command"] == str(prefix / "bin" / "rt-wait-inbox")
    assert stop_command["args"] == ["--claude-stop-hook"]
    assert stop_command["asyncRewake"] is True
    assert stop_command["timeout"] == harness_setup.CLAUDE_HOOK_TIMEOUT_SECONDS
    assert settings["permissions"]["allow"] == list(
        harness_setup._claude_permission_rules(prefix)
    )

    hermes = yaml.safe_load((home / ".hermes" / "config.yaml").read_text())
    assert hermes["plugins"]["enabled"] == ["pneu"]
    assert (
        os.readlink(home / ".hermes" / "plugins" / "pneu")
        == str(harness_setup._hermes_plugin_target(prefix))
    )

    for harness in harness_setup.HARNESSES:
        skill = home / f".{harness}" / "skills" / "pneu"
        assert skill.is_symlink()
        assert os.readlink(skill) == str(prefix / "skills" / "shared" / "pneu")

    for label in harness_setup.CODEX_LABELS:
        path = home / "Library" / "LaunchAgents" / f"{label}.plist"
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert plistlib.loads(path.read_bytes())["Label"] == label

    codex_hooks = json.loads((home / ".codex" / "hooks.json").read_text())
    codex_group = harness_setup._codex_groups(prefix)["SessionStart"]
    assert codex_hooks["hooks"]["SessionStart"].count(codex_group) == 1

    manifest_path = prefix / "harness-setup.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema"] == "roundtable.harness-setup.v1"
    claude_permissions = manifest["harnesses"]["claude"]["config"][
        "permissions"
    ]
    assert claude_permissions["permissions_container_added"] is True
    assert claude_permissions["allow_container_added"] is True
    assert all(item["added"] is True for item in claude_permissions["rules"])
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    marker_path = harness_setup._codex_reload_marker_path(prefix)
    marker = json.loads(marker_path.read_text())
    app_record = next(
        item
        for item in manifest["harnesses"]["codex"]["plists"]
        if item["label"] == "com.roundtable.codex-app-server"
    )
    assert marker == harness_setup._codex_reload_marker_value(
        prefix,
        Path(app_record["path"]),
        app_record["digest"],
    )
    assert stat.S_IMODE(marker_path.stat().st_mode) == 0o600
    lock_path = prefix / ".runtime" / "harness-setup.lock"
    assert lock_path.is_file()
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    first = _tree_snapshot(home)

    code, repeated = _run(
        capsys, home, prefix, "apply", *_all_harness_args()
    )
    assert code == 0
    assert repeated["writes"] is False
    assert _tree_snapshot(home) == first

    code, status = _run(
        capsys, home, prefix, "status", *_all_harness_args()
    )
    assert code == 0
    assert all(
        detail["state"] == "configured"
        for detail in status["harnesses"].values()
    )
    assert _tree_snapshot(home) == first

    code, refused = _run(
        capsys, home, prefix, "remove", *_all_harness_args()
    )
    assert code == 2
    assert "--unload-codex" in refused["error"]
    assert _tree_snapshot(home) == first

    launchctl = home / "fake-launchctl"
    _write_executable(
        launchctl,
        "#!/bin/sh\n"
        "if [ \"$1\" = print ]; then exit 113; fi\n"
        "exit 0\n",
    )
    monkeypatch.setenv("RT_LAUNCHCTL", str(launchctl))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    code, removed = _run(
        capsys,
        home,
        prefix,
        "remove",
        "--unload-codex",
        *_all_harness_args(),
    )
    assert code == 0
    assert removed["launchctl_invoked"] is True
    assert not manifest_path.exists()
    assert not (home / ".claude" / "settings.json").exists()
    assert not (home / ".hermes" / "config.yaml").exists()
    assert not (home / ".codex" / "hooks.json").exists()
    assert not marker_path.exists()
    for harness in harness_setup.HARNESSES:
        assert not (home / f".{harness}" / "skills" / "pneu").exists()
    assert not (home / ".hermes" / "plugins" / "pneu").exists()
    for label in harness_setup.CODEX_LABELS:
        assert not (
            home / "Library" / "LaunchAgents" / f"{label}.plist"
        ).exists()

    human_remove = harness_setup.main(
        ["remove", "--home", str(home), "--prefix", str(prefix)]
    )
    human_output = capsys.readouterr()
    assert human_remove == 0
    assert "preserved project registry" in human_output.out

    code, repeated_remove = _run(
        capsys, home, prefix, "remove", *_all_harness_args()
    )
    assert code == 0
    assert repeated_remove["writes"] is False

    # The launcher never had a chance to clear the first marker. Removal still
    # owns it completely, so a later clean re-add cannot see a foreign remnant.
    code, readded = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )
    assert code == 0
    assert readded["writes"] is True
    assert marker_path.is_file()


def test_existing_configuration_is_backed_up_and_preserved_exactly(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    claude_path = home / ".claude" / "settings.json"
    claude_path.parent.mkdir()
    groups = harness_setup._claude_groups(prefix)
    original_claude = {
        "model": "sonnet",
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "hooks": [{"type": "command", "command": "mine"}]},
                groups["SessionStart"],
            ],
            "Notification": [
                {"hooks": [{"type": "command", "command": "notify-me"}]}
            ],
        },
    }
    claude_raw = (json.dumps(original_claude, indent=4) + "\n").encode()
    claude_path.write_bytes(claude_raw)

    hermes_path = home / ".hermes" / "config.yaml"
    hermes_path.parent.mkdir()
    hermes_raw = (
        "# keep this user's comment\n"
        "model: llama\n"
        "plugins:\n"
        "  enabled:\n"
        "    - personal\n"
        "  disabled:\n"
        "    - unrelated\n"
    ).encode()
    hermes_path.write_bytes(hermes_raw)

    # Correct pre-existing links are observed, never claimed.
    claude_skill = home / ".claude" / "skills" / "pneu"
    claude_skill.parent.mkdir(parents=True)
    claude_skill.symlink_to(str(harness_setup._skill_target(prefix)))
    hermes_plugin = home / ".hermes" / "plugins" / "pneu"
    hermes_plugin.parent.mkdir(parents=True)
    hermes_plugin.symlink_to(str(harness_setup._hermes_plugin_target(prefix)))

    code, _result = _run(
        capsys,
        home,
        prefix,
        "apply",
        "--harness",
        "claude",
        "--harness",
        "hermes",
    )
    assert code == 0

    manifest = json.loads((prefix / "harness-setup.json").read_text())
    claude_record = manifest["harnesses"]["claude"]
    hermes_record = manifest["harnesses"]["hermes"]
    assert claude_record["skill"]["added"] is False
    assert hermes_record["plugin"]["added"] is False
    assert claude_record["config"]["fragments"][0]["added"] is False
    assert hermes_record["config"]["enabled_added"] is True

    backups = list((prefix / "backups" / "harness-setup").iterdir())
    assert len(backups) == 2
    assert {path.read_bytes() for path in backups} == {claude_raw, hermes_raw}
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in backups)
    assert (
        stat.S_IMODE((prefix / "backups" / "harness-setup").stat().st_mode)
        == 0o700
    )

    code, _removed = _run(
        capsys,
        home,
        prefix,
        "remove",
        "--harness",
        "claude",
        "--harness",
        "hermes",
    )
    assert code == 0
    remaining_claude = json.loads(claude_path.read_text())
    assert remaining_claude["model"] == "sonnet"
    assert groups["SessionStart"] in remaining_claude["hooks"]["SessionStart"]
    assert remaining_claude["hooks"]["Notification"] == original_claude["hooks"][
        "Notification"
    ]
    assert hermes_path.read_bytes() == hermes_raw
    remaining_hermes = yaml.safe_load(hermes_path.read_text())
    assert remaining_hermes["model"] == "llama"
    assert remaining_hermes["plugins"]["enabled"] == ["personal"]
    assert claude_skill.is_symlink()
    assert hermes_plugin.is_symlink()


def test_claude_permissions_preserve_preexisting_rules_on_remove(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    rules = harness_setup._claude_permission_rules(prefix)
    path = home / ".claude" / "settings.json"
    path.parent.mkdir()
    original = {
        "permissions": {
            "allow": [rules[0], "Bash(rt-wait-inbox:*)"],
            "defaultMode": "default",
        }
    }
    path.write_text(json.dumps(original))

    code, _result = _run(
        capsys, home, prefix, "apply", "--harness", "claude"
    )
    assert code == 0
    manifest = json.loads((prefix / "harness-setup.json").read_text())
    permission_record = manifest["harnesses"]["claude"]["config"][
        "permissions"
    ]
    assert [item["added"] for item in permission_record["rules"]] == [
        False,
        True,
        True,
    ]

    code, _removed = _run(
        capsys, home, prefix, "remove", "--harness", "claude"
    )
    assert code == 0
    assert json.loads(path.read_text()) == original


def test_claude_legacy_stop_and_permissions_upgrade_in_place(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    code, _result = _run(
        capsys, home, prefix, "apply", "--harness", "claude"
    )
    assert code == 0

    settings_path = home / ".claude" / "settings.json"
    manifest_path = prefix / "harness-setup.json"
    settings = json.loads(settings_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    current_stop = harness_setup._claude_groups(prefix)["Stop"]
    legacy_stop = harness_setup._legacy_claude_groups(prefix)["Stop"][0]
    assert settings["hooks"]["Stop"] == [current_stop]
    settings["hooks"]["Stop"] = [legacy_stop]
    settings.pop("permissions")
    claude_config = manifest["harnesses"]["claude"]["config"]
    stop_record = next(
        item for item in claude_config["fragments"] if item["event"] == "Stop"
    )
    stop_record["group"] = legacy_stop
    claude_config.pop("permissions")
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    before = _tree_snapshot(home)
    code, status = _run(
        capsys, home, prefix, "status", "--harness", "claude"
    )
    assert code == 0
    assert status["harnesses"]["claude"]["state"] == "upgrade_required"
    assert _tree_snapshot(home) == before

    code, plan = _run(
        capsys, home, prefix, "plan", "--harness", "claude"
    )
    assert code == 0
    assert plan["writes"] is False
    assert plan["harnesses"]["claude"]["state"] == "upgrade_planned"
    assert len(
        [
            action
            for action in plan["harnesses"]["claude"]["actions"]
            if action.startswith("allow Bash(")
        ]
    ) == 3
    assert _tree_snapshot(home) == before

    code, applied = _run(
        capsys, home, prefix, "apply", "--harness", "claude"
    )
    assert code == 0
    assert applied["writes"] is True
    upgraded = json.loads(settings_path.read_text())
    assert upgraded["hooks"]["Stop"] == [current_stop]
    assert upgraded["permissions"]["allow"] == list(
        harness_setup._claude_permission_rules(prefix)
    )
    upgraded_manifest = json.loads(manifest_path.read_text())
    upgraded_stop = next(
        item
        for item in upgraded_manifest["harnesses"]["claude"]["config"][
            "fragments"
        ]
        if item["event"] == "Stop"
    )
    assert upgraded_stop["group"] == current_stop
    assert upgraded_stop["added"] is True

    code, repeated = _run(
        capsys, home, prefix, "apply", "--harness", "claude"
    )
    assert code == 0
    assert repeated["writes"] is False


def test_claude_legacy_preexisting_stop_is_not_claimed_for_upgrade(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    code, _result = _run(
        capsys, home, prefix, "apply", "--harness", "claude"
    )
    assert code == 0
    settings_path = home / ".claude" / "settings.json"
    manifest_path = prefix / "harness-setup.json"
    settings = json.loads(settings_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    legacy_stop = harness_setup._legacy_claude_groups(prefix)["Stop"][0]
    settings["hooks"]["Stop"] = [legacy_stop]
    settings.pop("permissions")
    config = manifest["harnesses"]["claude"]["config"]
    config["created"] = False
    config["hooks_container_added"] = False
    config["event_containers_added"]["Stop"] = False
    stop_record = next(
        item for item in config["fragments"] if item["event"] == "Stop"
    )
    stop_record["group"] = legacy_stop
    stop_record["added"] = False
    config.pop("permissions")
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    before = _tree_snapshot(home)

    code, result = _run(
        capsys, home, prefix, "plan", "--harness", "claude"
    )

    assert code == 2
    assert "cannot replace pre-existing legacy Claude Stop hook" in result["error"]
    assert _tree_snapshot(home) == before


def test_claude_owned_permission_drift_fails_closed(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    code, _result = _run(
        capsys, home, prefix, "apply", "--harness", "claude"
    )
    assert code == 0
    path = home / ".claude" / "settings.json"
    settings = json.loads(path.read_text())
    settings["permissions"]["allow"].remove(
        harness_setup._claude_permission_rules(prefix)[1]
    )
    path.write_text(json.dumps(settings))
    before = _tree_snapshot(home)

    code, status = _run(
        capsys, home, prefix, "status", "--harness", "claude"
    )
    assert code == 2
    assert "managed Claude permission drift" in status["error"]
    assert _tree_snapshot(home) == before

    code, removed = _run(
        capsys, home, prefix, "remove", "--harness", "claude"
    )
    assert code == 2
    assert "managed Claude permission drift" in removed["error"]
    assert _tree_snapshot(home) == before


@pytest.mark.parametrize("policy_rule", ["Bash", "Bash(*)"])
def test_claude_permission_conflict_fails_before_writes(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    policy_rule: str,
) -> None:
    home, prefix = installation
    path = home / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"permissions": {"ask": [policy_rule]}}))
    before = _tree_snapshot(home)

    code, result = _run(
        capsys, home, prefix, "apply", "--harness", "claude"
    )

    assert code == 2
    assert "ask/deny rules override" in result["error"]
    assert "/permissions" in result["error"]
    assert _tree_snapshot(home) == before


def test_claude_disabled_hooks_fail_before_writes(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    path = home / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"disableAllHooks": True}))
    before = _tree_snapshot(home)

    code, result = _run(
        capsys, home, prefix, "apply", "--harness", "claude"
    )

    assert code == 2
    assert "disableAllHooks=true" in result["error"]
    assert "/hooks" in result["error"]
    assert _tree_snapshot(home) == before


def test_unrelated_changes_survive_exact_fragment_removal(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    code, _ = _run(
        capsys, home, prefix, "apply", "--harness", "claude"
    )
    assert code == 0
    path = home / ".claude" / "settings.json"
    settings = json.loads(path.read_text())
    settings["theme"] = "dark"
    settings["hooks"]["Notification"] = [
        {"hooks": [{"type": "command", "command": "user-command"}]}
    ]
    path.write_text(json.dumps(settings))

    code, _ = _run(
        capsys, home, prefix, "remove", "--harness", "claude"
    )

    assert code == 0
    remaining = json.loads(path.read_text())
    assert remaining == {
        "theme": "dark",
        "hooks": {
            "Notification": [
                {"hooks": [{"type": "command", "command": "user-command"}]}
            ]
        },
    }


def test_drift_fails_closed_without_partial_removal(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    code, _ = _run(
        capsys,
        home,
        prefix,
        "apply",
        "--harness",
        "claude",
        "--harness",
        "hermes",
    )
    assert code == 0
    settings_path = home / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["hooks"]["Stop"][0]["hooks"][0]["command"] = "/tmp/replaced"
    settings_path.write_text(json.dumps(settings))
    plugin_link = home / ".hermes" / "plugins" / "pneu"
    assert plugin_link.is_symlink()

    code, result = _run(
        capsys,
        home,
        prefix,
        "remove",
        "--harness",
        "claude",
        "--harness",
        "hermes",
    )

    assert code == 2
    assert result["ok"] is False
    assert "drift" in result["error"]
    assert plugin_link.is_symlink()
    assert (prefix / "harness-setup.json").is_file()


def test_symlinked_config_and_colliding_managed_link_fail_closed(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    outside = home / "outside.json"
    outside.write_text("{}")
    claude_dir = home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").symlink_to(outside)
    before = _tree_snapshot(home)

    code, result = _run(
        capsys, home, prefix, "plan", "--harness", "claude"
    )

    assert code == 2
    assert "symlink" in result["error"]
    assert _tree_snapshot(home) == before

    (claude_dir / "settings.json").unlink()
    skill = claude_dir / "skills" / "pneu"
    skill.parent.mkdir()
    skill.write_text("user data")
    code, result = _run(
        capsys, home, prefix, "apply", "--harness", "claude"
    )
    assert code == 2
    assert "non-symlink" in result["error"]
    assert skill.read_text() == "user data"
    assert not (prefix / "harness-setup.json").exists()


def test_hermes_explicit_disable_is_respected_without_writes(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    config = home / ".hermes" / "config.yaml"
    config.parent.mkdir()
    config.write_text("plugins:\n  disabled:\n    - pneu\n")
    before = _tree_snapshot(home)

    code, result = _run(
        capsys, home, prefix, "apply", "--harness", "hermes"
    )

    assert code == 2
    assert "explicitly disabled" in result["error"]
    assert _tree_snapshot(home) == before


def test_duplicate_preexisting_fragments_are_rejected_without_writes(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    claude = home / ".claude" / "settings.json"
    claude.parent.mkdir()
    group = harness_setup._claude_groups(prefix)["SessionStart"]
    claude.write_text(
        json.dumps({"hooks": {"SessionStart": [group, group]}})
    )
    before = _tree_snapshot(home)

    code, result = _run(
        capsys, home, prefix, "apply", "--harness", "claude"
    )

    assert code == 2
    assert "duplicate pneu fragments" in result["error"]
    assert _tree_snapshot(home) == before

    claude.unlink()
    hermes = home / ".hermes" / "config.yaml"
    hermes.parent.mkdir()
    hermes.write_text(
        "plugins:\n  enabled:\n    - pneu\n    - pneu\n"
    )
    before = _tree_snapshot(home)

    code, result = _run(
        capsys, home, prefix, "apply", "--harness", "hermes"
    )

    assert code == 2
    assert "duplicate 'pneu'" in result["error"]
    assert _tree_snapshot(home) == before


def test_different_existing_codex_plist_is_never_overwritten(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    path = launch_agents / "com.roundtable.codex-app-server.plist"
    foreign = plistlib.dumps(
        {
            "Label": "com.roundtable.codex-app-server",
            "ProgramArguments": ["/usr/bin/false"],
        }
    )
    path.write_bytes(foreign)

    code, result = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )

    assert code == 2
    assert "non-owned" in result["error"]
    assert path.read_bytes() == foreign
    assert not (
        launch_agents / "com.roundtable.codex-wake.plist"
    ).exists()
    assert not (prefix / "harness-setup.json").exists()


def test_exact_existing_codex_plists_are_adopted_with_private_modes(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    payloads = harness_setup._codex_payloads(
        home,
        prefix,
        ensure_runtime=False,
    )
    paths: list[Path] = []
    for label in harness_setup.CODEX_LABELS:
        path = launch_agents / f"{label}.plist"
        path.write_bytes(
            plistlib.dumps(payloads[label], fmt=plistlib.FMT_XML, sort_keys=True)
        )
        path.chmod(0o644)
        paths.append(path)

    code, configured = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )

    assert code == 0, configured
    assert configured["writes"] is True
    assert configured["restart_required"] is False
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)
    assert not harness_setup._codex_reload_marker_path(prefix).exists()

    code, status = _run(
        capsys, home, prefix, "status", "--harness", "codex"
    )
    assert code == 0
    assert status["harnesses"]["codex"]["state"] == "configured"


def test_owned_codex_plist_mode_drift_is_planned_and_repaired_without_reload(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    code, configured = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )
    assert code == 0, configured
    marker = harness_setup._codex_reload_marker_path(prefix)
    marker.unlink()
    app_path = (
        home
        / "Library"
        / "LaunchAgents"
        / "com.roundtable.codex-app-server.plist"
    )
    original = app_path.read_bytes()
    app_path.chmod(0o644)

    code, planned = _run(
        capsys, home, prefix, "plan", "--harness", "codex"
    )
    assert code == 0
    assert planned["writes"] is False
    assert planned["harnesses"]["codex"]["state"] == "upgrade_planned"
    assert planned["harnesses"]["codex"]["actions"] == [
        f"secure {app_path} permissions to 0600"
    ]
    assert stat.S_IMODE(app_path.stat().st_mode) == 0o644

    code, repaired = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )
    assert code == 0, repaired
    assert repaired["writes"] is True
    assert repaired["restart_required"] is False
    assert app_path.read_bytes() == original
    assert stat.S_IMODE(app_path.stat().st_mode) == 0o600
    assert not marker.exists()

    code, repeated = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )
    assert code == 0
    assert repeated["writes"] is False


def test_codex_hook_merge_and_remove_preserve_unrelated_groups(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation
    path = home / ".codex" / "hooks.json"
    path.parent.mkdir()
    user_group = {
        "hooks": [
            {"type": "command", "command": "/usr/bin/true", "timeout": 3}
        ]
    }
    original = {
        "hooks": {
            "SessionStart": [user_group],
            "Stop": [{"hooks": [{"type": "command", "command": "mine"}]}],
        },
        "userSetting": True,
    }
    path.write_text(json.dumps(original, indent=4) + "\n")

    code, configured = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )

    assert code == 0
    assert configured["writes"] is True
    value = json.loads(path.read_text())
    group = harness_setup._codex_groups(prefix)["SessionStart"]
    assert value["hooks"]["SessionStart"] == [user_group, group]
    assert value["hooks"]["Stop"] == original["hooks"]["Stop"]
    assert value["userSetting"] is True
    manifest = json.loads((prefix / "harness-setup.json").read_text())
    assert manifest["harnesses"]["codex"]["config"]["fragments"][0]["added"]

    launchctl = home / "fake-launchctl"
    _write_executable(
        launchctl,
        "#!/bin/sh\n"
        "if [ \"$1\" = print ]; then exit 113; fi\n"
        "exit 0\n",
    )
    monkeypatch.setenv("RT_LAUNCHCTL", str(launchctl))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    code, removed = _run(
        capsys,
        home,
        prefix,
        "remove",
        "--unload-codex",
        "--harness",
        "codex",
    )

    assert code == 0
    assert removed["writes"] is True
    assert json.loads(path.read_text()) == original


def test_owned_codex_plists_upgrade_only_from_manifest_digest(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation
    code, _configured = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )
    assert code == 0
    app_path = (
        home
        / "Library"
        / "LaunchAgents"
        / "com.roundtable.codex-app-server.plist"
    )
    old_payload = app_path.read_bytes()
    marker_path = harness_setup._codex_reload_marker_path(prefix)
    old_marker = marker_path.read_bytes()
    original_builder = harness_setup._codex_payloads

    def upgraded_payloads(home_arg, prefix_arg, *, ensure_runtime):
        payloads = original_builder(
            home_arg,
            prefix_arg,
            ensure_runtime=ensure_runtime,
        )
        payloads["com.roundtable.codex-app-server"]["ThrottleInterval"] = 17
        return payloads

    monkeypatch.setattr(harness_setup, "_codex_payloads", upgraded_payloads)
    before_plan = _tree_snapshot(home)
    code, planned = _run(
        capsys, home, prefix, "plan", "--harness", "codex"
    )
    assert code == 0
    assert planned["harnesses"]["codex"]["state"] == "upgrade_planned"
    assert any(
        "update" in action
        for action in planned["harnesses"]["codex"]["actions"]
    )
    assert _tree_snapshot(home) == before_plan

    code, applied = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )
    assert code == 0, applied
    assert applied["restart_required"] is True
    assert app_path.read_bytes() != old_payload
    assert plistlib.loads(app_path.read_bytes())["ThrottleInterval"] == 17
    assert marker_path.read_bytes() != old_marker
    upgraded_manifest = json.loads((prefix / "harness-setup.json").read_text())
    upgraded_record = next(
        item
        for item in upgraded_manifest["harnesses"]["codex"]["plists"]
        if item["label"] == "com.roundtable.codex-app-server"
    )
    assert json.loads(marker_path.read_text()) == (
        harness_setup._codex_reload_marker_value(
            prefix,
            app_path,
            upgraded_record["digest"],
        )
    )

    code, repeated = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )
    assert code == 0
    assert repeated["writes"] is False


def test_previous_managed_codex_record_is_upgraded_with_session_start_hook(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, prefix = installation
    code, _configured = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )
    assert code == 0
    manifest_path = prefix / "harness-setup.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["harnesses"]["codex"]["config"]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    hook_path = home / ".codex" / "hooks.json"
    hook_path.unlink()

    before = _tree_snapshot(home)
    code, status = _run(
        capsys, home, prefix, "status", "--harness", "codex"
    )
    assert code == 0
    assert status["harnesses"]["codex"]["state"] == "upgrade_required"
    assert _tree_snapshot(home) == before

    code, upgraded = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )
    assert code == 0
    assert upgraded["writes"] is True
    assert upgraded["restart_required"] is False
    group = harness_setup._codex_groups(prefix)["SessionStart"]
    hooks = json.loads(hook_path.read_text())
    assert hooks["hooks"]["SessionStart"] == [group]
    updated = json.loads(manifest_path.read_text())
    assert "config" in updated["harnesses"]["codex"]


def test_codex_remove_unloads_exact_jobs_and_refuses_inside_codex(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation
    code, _result = _run(
        capsys, home, prefix, "apply", "--harness", "codex"
    )
    assert code == 0

    log = home / "launchctl.log"
    launchctl = home / "fake-launchctl"
    _write_executable(
        launchctl,
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$RT_TEST_LAUNCHCTL_LOG\"\n"
        "exit 0\n",
    )
    monkeypatch.setenv("RT_LAUNCHCTL", str(launchctl))
    monkeypatch.setenv("RT_TEST_LAUNCHCTL_LOG", str(log))
    monkeypatch.setenv("CODEX_THREAD_ID", "current-codex-thread")

    code, refused = _run(
        capsys,
        home,
        prefix,
        "remove",
        "--unload-codex",
        "--harness",
        "codex",
    )
    assert code == 2
    assert "outside Codex" in refused["error"]
    assert not log.exists()
    assert (prefix / "harness-setup.json").is_file()

    monkeypatch.delenv("CODEX_THREAD_ID")
    code, removed = _run(
        capsys,
        home,
        prefix,
        "remove",
        "--unload-codex",
        "--harness",
        "codex",
    )
    assert code == 0
    assert removed["launchctl_invoked"] is True
    expected = []
    domain = f"gui/{os.getuid()}"
    for label in harness_setup.CODEX_LABELS:
        expected.extend(
            [f"print {domain}/{label}", f"bootout {domain}/{label}"]
        )
    assert log.read_text().splitlines() == expected


def test_partial_codex_unload_never_claims_a_full_rollback(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation
    code, _result = _run(
        capsys,
        home,
        prefix,
        "apply",
        "--harness",
        "codex",
    )
    assert code == 0
    domain = f"gui/{os.getuid()}"
    app_server, wake = harness_setup.CODEX_LABELS
    log = home / "launchctl-partial.log"
    launchctl = home / "partial-launchctl"
    _write_executable(
        launchctl,
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$RT_TEST_LAUNCHCTL_LOG\"\n"
        "case \"$1 $2\" in\n"
        f"  \"print {domain}/{app_server}\") exit 0 ;;\n"
        f"  \"bootout {domain}/{app_server}\") exit 0 ;;\n"
        f"  \"print {domain}/{wake}\") exit 9 ;;\n"
        "esac\n"
        "exit 9\n",
    )
    monkeypatch.setenv("RT_LAUNCHCTL", str(launchctl))
    monkeypatch.setenv("RT_TEST_LAUNCHCTL_LOG", str(log))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    before_manifest = (prefix / "harness-setup.json").read_bytes()
    before_plists = {
        label: (
            home / "Library" / "LaunchAgents" / f"{label}.plist"
        ).read_bytes()
        for label in harness_setup.CODEX_LABELS
    }

    code, result = _run(
        capsys,
        home,
        prefix,
        "remove",
        "--unload-codex",
        "--harness",
        "codex",
    )

    assert code == 2
    assert f"cannot inspect LaunchAgent {wake}" in result["error"]
    assert result["launchctl_invoked"] is True
    assert result["writes"] is True
    assert result["rolled_back"] is False
    assert (prefix / "harness-setup.json").read_bytes() == before_manifest
    for label, payload in before_plists.items():
        assert (
            home / "Library" / "LaunchAgents" / f"{label}.plist"
        ).read_bytes() == payload
    assert log.read_text().splitlines() == [
        f"print {domain}/{app_server}",
        f"bootout {domain}/{app_server}",
        f"print {domain}/{wake}",
    ]


@pytest.mark.parametrize("command", ["plan", "apply"])
def test_at_or_above_floor_codex_release_is_accepted_without_probe(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    # Setup gates on the version floor only and never probes; any release at or
    # above the floor is accepted here and proven later by the launch/wake
    # protocol probe.
    home, prefix = installation
    monkeypatch.delenv("RT_CODEX_ALLOW_UNVALIDATED", raising=False)
    _write_executable(
        home / ".npm-global" / "bin" / "codex",
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"--version\" ]; then\n"
        "  printf '%s\\n' 'codex-cli 0.144.7'\n"
        "fi\n"
        "exit 0\n",
    )

    code, result = _run(
        capsys,
        home,
        prefix,
        command,
        "--harness",
        "codex",
    )

    assert code == 0
    expected_state = "configured" if command == "apply" else "planned"
    assert result["harnesses"]["codex"]["state"] == expected_state


@pytest.mark.parametrize("command", ["plan", "apply"])
def test_existing_codex_setup_revalidates_cli_before_plan_or_apply(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    home, prefix = installation
    monkeypatch.delenv("RT_CODEX_ALLOW_UNVALIDATED", raising=False)
    code, configured = _run(
        capsys,
        home,
        prefix,
        "apply",
        "--harness",
        "codex",
    )
    assert code == 0
    assert configured["harnesses"]["codex"]["state"] == "configured"

    # Regressing the CLI below the floor must still fail closed on revalidation.
    _write_executable(
        home / ".npm-global" / "bin" / "codex",
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"--version\" ]; then\n"
        "  printf '%s\\n' 'codex-cli 0.143.0'\n"
        "fi\n"
        "exit 0\n",
    )
    before = _tree_snapshot(home)

    code, result = _run(
        capsys,
        home,
        prefix,
        command,
        "--harness",
        "codex",
    )

    assert code == 2
    assert "below the minimum" in result["error"]
    assert result["writes"] is False
    assert result["rolled_back"] is False
    assert result["launchctl_invoked"] is False
    assert _tree_snapshot(home) == before


def test_above_floor_codex_release_plans_and_applies_without_valve(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation
    monkeypatch.delenv("RT_CODEX_ALLOW_UNVALIDATED", raising=False)
    _write_executable(
        home / ".npm-global" / "bin" / "codex",
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"--version\" ]; then\n"
        "  printf '%s\\n' 'codex-cli 0.145.0'\n"
        "fi\n"
        "exit 0\n",
    )

    code, planned = _run(
        capsys,
        home,
        prefix,
        "plan",
        "--harness",
        "codex",
    )
    assert code == 0
    assert planned["harnesses"]["codex"]["state"] == "planned"

    code, applied = _run(
        capsys,
        home,
        prefix,
        "apply",
        "--harness",
        "codex",
    )
    assert code == 0
    assert applied["harnesses"]["codex"]["state"] == "configured"
    wake_plist_path = (
        home / "Library" / "LaunchAgents" / "com.roundtable.codex-wake.plist"
    )
    payload = plistlib.loads(wake_plist_path.read_bytes())
    # No release valve exists any more, so nothing carries it into the plist.
    assert "RT_CODEX_ALLOW_UNVALIDATED" not in payload["EnvironmentVariables"]


def test_below_floor_codex_release_is_rejected(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation
    monkeypatch.delenv("RT_CODEX_ALLOW_UNVALIDATED", raising=False)
    _write_executable(
        home / ".npm-global" / "bin" / "codex",
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"--version\" ]; then\n"
        "  printf '%s\\n' 'codex-cli 0.143.0'\n"
        "fi\n"
        "exit 0\n",
    )
    before = _tree_snapshot(home)

    code, result = _run(
        capsys,
        home,
        prefix,
        "plan",
        "--harness",
        "codex",
    )

    assert code == 2
    assert "below the minimum" in result["error"]
    assert result["writes"] is False
    assert _tree_snapshot(home) == before
    assert not (prefix / ".runtime").exists()
    assert not (prefix / "harness-setup.json").exists()


def test_apply_preflights_every_mutation_parent_before_writing(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation
    blocked = home / ".claude" / "skills"
    blocked.mkdir(parents=True)
    before = _tree_snapshot(home)
    real_access = harness_setup.os.access

    def access(path: str | Path, mode: int) -> bool:
        if Path(path) == blocked and mode == os.W_OK | os.X_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(harness_setup.os, "access", access)
    code, result = _run(
        capsys,
        home,
        prefix,
        "apply",
        "--harness",
        "claude",
    )

    assert code == 2
    assert "not writable and searchable" in result["error"]
    assert result["writes"] is False
    assert result["rolled_back"] is False
    assert _tree_snapshot(home) == before
    assert not (prefix / ".runtime").exists()
    assert not (prefix / "harness-setup.json").exists()


def test_remove_preflights_every_mutation_parent_before_deleting(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation
    code, _result = _run(
        capsys,
        home,
        prefix,
        "apply",
        "--harness",
        "claude",
    )
    assert code == 0
    blocked = home / ".claude" / "skills"
    before = _tree_snapshot(home)
    real_access = harness_setup.os.access

    def access(path: str | Path, mode: int) -> bool:
        if Path(path) == blocked and mode == os.W_OK | os.X_OK:
            return False
        return real_access(path, mode)

    monkeypatch.setattr(harness_setup.os, "access", access)
    code, result = _run(
        capsys,
        home,
        prefix,
        "remove",
        "--harness",
        "claude",
    )

    assert code == 2
    assert "not writable and searchable" in result["error"]
    assert result["writes"] is False
    assert result["rolled_back"] is False
    assert _tree_snapshot(home) == before


def test_apply_failure_rolls_back_configs_links_plists_backups_and_manifest(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation
    claude_path = home / ".claude" / "settings.json"
    claude_path.parent.mkdir()
    claude_raw = b'{\n  "theme": "dark"\n}\n'
    claude_path.write_bytes(claude_raw)
    hermes_path = home / ".hermes" / "config.yaml"
    hermes_path.parent.mkdir()
    hermes_raw = b"# preserve me\nmodel: llama\n"
    hermes_path.write_bytes(hermes_raw)
    original_write_manifest = harness_setup._write_manifest

    def write_manifest_then_fail(path_prefix: Path, manifest: dict) -> None:
        original_write_manifest(path_prefix, manifest)
        raise OSError("injected failure after manifest write")

    monkeypatch.setattr(
        harness_setup,
        "_write_manifest",
        write_manifest_then_fail,
    )
    code, result = _run(
        capsys,
        home,
        prefix,
        "apply",
        *_all_harness_args(),
    )

    assert code == 2
    assert "injected failure after manifest write" in result["error"]
    assert result["writes"] is True
    assert result["rolled_back"] is True
    assert result["launchctl_invoked"] is False
    assert claude_path.read_bytes() == claude_raw
    assert hermes_path.read_bytes() == hermes_raw
    assert not (prefix / "harness-setup.json").exists()
    backup_root = prefix / "backups" / "harness-setup"
    assert not backup_root.exists() or not any(backup_root.iterdir())
    for harness in harness_setup.HARNESSES:
        assert not (home / f".{harness}" / "skills" / "pneu").exists()
    assert not (home / ".hermes" / "plugins" / "pneu").exists()
    for label in harness_setup.CODEX_LABELS:
        assert not (
            home / "Library" / "LaunchAgents" / f"{label}.plist"
        ).exists()
    assert not harness_setup._codex_reload_marker_path(prefix).exists()


def test_remove_failure_restores_exact_configured_state(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation
    code, _result = _run(
        capsys,
        home,
        prefix,
        "apply",
        "--harness",
        "claude",
        "--harness",
        "hermes",
    )
    assert code == 0
    before = _tree_snapshot(home)
    original_remove_record = harness_setup._remove_record

    def remove_then_fail(
        harness: str,
        selected_home: Path,
        record: dict,
    ) -> None:
        original_remove_record(harness, selected_home, record)
        if harness == "hermes":
            raise OSError("injected failure after Hermes removal")

    monkeypatch.setattr(harness_setup, "_remove_record", remove_then_fail)
    code, result = _run(
        capsys,
        home,
        prefix,
        "remove",
        "--harness",
        "claude",
        "--harness",
        "hermes",
    )

    assert code == 2
    assert "injected failure after Hermes removal" in result["error"]
    assert result["writes"] is True
    assert result["rolled_back"] is True
    assert result["launchctl_invoked"] is False
    assert _tree_snapshot(home) == before


def test_failed_rollback_is_reported_without_claiming_success(
    installation: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, prefix = installation
    original_write_manifest = harness_setup._write_manifest
    original_restore_snapshot = harness_setup._restore_snapshot

    def write_manifest_then_fail(path_prefix: Path, manifest: dict) -> None:
        original_write_manifest(path_prefix, manifest)
        raise OSError("injected apply failure")

    def fail_one_restore(snapshot: harness_setup._PathSnapshot) -> None:
        if snapshot.path == home / ".claude" / "settings.json":
            raise OSError("injected rollback failure")
        original_restore_snapshot(snapshot)

    monkeypatch.setattr(
        harness_setup,
        "_write_manifest",
        write_manifest_then_fail,
    )
    monkeypatch.setattr(
        harness_setup,
        "_restore_snapshot",
        fail_one_restore,
    )
    code, result = _run(
        capsys,
        home,
        prefix,
        "apply",
        "--harness",
        "claude",
    )

    assert code == 2
    assert result["writes"] is True
    assert result["rolled_back"] is False
    assert "injected rollback failure" in result["error"]
    assert (home / ".claude" / "settings.json").is_file()
