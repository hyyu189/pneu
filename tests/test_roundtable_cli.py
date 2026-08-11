import importlib.machinery
import importlib.util
import io
import json
import os
import pty
import select
import sys
import threading
import tty
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

from _rtlib import register_project


def load_script():
    loader = importlib.machinery.SourceFileLoader(
        "roundtable_unified_cli", str(BIN / "roundtable")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


roundtable = load_script()


class TTYInput(io.StringIO):
    def isatty(self):
        return True


def run_with_pty(callback, input_bytes: bytes):
    master, slave = pty.openpty()
    tty.setcbreak(slave)
    stdin = os.fdopen(os.dup(slave), "r", encoding="utf-8", buffering=1)
    stderr = os.fdopen(os.dup(slave), "w", encoding="utf-8", buffering=1)
    output = bytearray()
    stopped = threading.Event()

    def drain_master():
        while not stopped.is_set():
            ready, _write, _error = select.select([master], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)

    reader = threading.Thread(target=drain_master, daemon=True)
    reader.start()
    os.write(master, input_bytes)
    try:
        result = callback(stdin, stderr)
        stderr.flush()
    finally:
        stdin.close()
        stderr.close()
        os.close(slave)
        stopped.set()
        reader.join(timeout=1)
    os.close(master)
    return result, output.decode(errors="replace")


def write_project(path: Path, seats=None) -> Path:
    seats = seats or {"codex": ("codex", ["codex"])}
    state = path / ".roundtable"
    state.mkdir(parents=True)
    lines = [
        "schema: roundtable.agents.v1",
        f"project: {path.resolve()}",
        "agents:",
    ]
    for name, (harness, instance_ids) in seats.items():
        lines.extend(
            [
                f"  {name}:",
                f"    harness: {harness}",
                "    instances:",
            ]
        )
        lines.extend(f"      - id: {instance_id}" for instance_id in instance_ids)
    (state / "agents.yaml").write_text("\n".join(lines) + "\n")
    return path.resolve()


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    registry = tmp_path / "projects.yaml"
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    return registry


@pytest.fixture
def fake_commands(monkeypatch, tmp_path):
    command_dir = tmp_path / "commands"
    command_dir.mkdir()

    def resolve(name):
        return command_dir / name

    monkeypatch.setattr(roundtable, "sibling", resolve)
    monkeypatch.setattr(
        roundtable,
        "harness_bin",
        lambda harness: command_dir / harness,
    )
    return command_dir


@pytest.mark.parametrize(
    ("alias", "target"),
    sorted(roundtable.ALIASES.items()),
)
def test_scriptable_aliases_pass_every_argument_through(
    alias, target, fake_commands, tmp_path
):
    calls = []

    def fake_exec(path, argv):
        calls.append((path, argv))
        return 0

    result = roundtable.main(
        [alias, "--example", "two words"],
        cwd=tmp_path,
        home=tmp_path / "home",
        exec_runner=fake_exec,
    )

    expected = fake_commands / target
    assert result == 0
    assert calls == [
        (
            str(expected),
            [str(expected), "--example", "two words"],
        )
    ]


def test_no_argument_non_tty_fails_with_help_without_exec(fake_commands, tmp_path):
    stderr = io.StringIO()
    calls = []

    result = roundtable.main(
        [],
        cwd=tmp_path,
        home=tmp_path / "home",
        stdin=io.StringIO(),
        stderr=stderr,
        exec_runner=lambda *args: calls.append(args),
    )

    assert result == 2
    assert "stdin is not a TTY" in stderr.getvalue()
    assert "usage: pneu" in stderr.getvalue()
    assert calls == []


def test_guide_command_renders_ascii_mailroom_and_wake_model(tmp_path):
    stdout = io.StringIO()

    result = roundtable.main(
        ["guide"],
        cwd=tmp_path,
        home=tmp_path / "home",
        stdout=stdout,
        stderr=io.StringIO(),
    )

    rendered = stdout.getvalue()
    assert result == 0
    assert "pneu = a local mailroom for coding-agent seats" in rendered
    assert "project mailbox: new/  ->  cur/" in rendered
    assert "Claude  SessionStart/Stop hooks" in rendered
    assert "Hermes  the session-start plugin" in rendered
    assert "Codex   the app-server and Unix-socket notification bridge" in rendered
    assert "OpenClaw  the isolated Gateway seat" in rendered
    assert "Grok Build the isolated ACP seat" in rendered


def test_version_command_reports_manifest_prefix_and_current_target(
    tmp_path, monkeypatch
):
    prefix = tmp_path / "install"
    (prefix / "versions" / "0.2.1").mkdir(parents=True)
    (prefix / "current").symlink_to("versions/0.2.1")
    (prefix / "install-manifest.json").write_text(
        json.dumps({"version": "0.2.1"})
    )
    monkeypatch.setenv("ROUNDTABLE_INSTALL_PREFIX", str(prefix))
    stdout = io.StringIO()

    result = roundtable.main(
        ["version"],
        cwd=tmp_path,
        home=tmp_path / "home",
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert result == 0
    rendered = stdout.getvalue()
    assert "version: 0.2.1" in rendered
    assert f"install prefix: {prefix}" in rendered
    assert "current target: versions/0.2.1" in rendered


def test_project_menu_reprompts_after_invalid_numeric_input(
    tmp_path, isolated_registry
):
    project = write_project(tmp_path / "registered")
    register_project(project, isolated_registry)
    cwd = tmp_path / "outside"
    cwd.mkdir()
    stderr = io.StringIO()

    selected = roundtable.choose_project(
        cwd=cwd,
        home=tmp_path / "home",
        stdin=TTYInput("0\n1\n1\n"),
        stderr=stderr,
    )

    assert selected == project
    assert "please try again" in stderr.getvalue()


def test_line_oriented_onboarding_does_not_auto_print_the_full_guide(
    tmp_path, isolated_registry, fake_commands
):
    home = tmp_path / "home"
    home.mkdir()
    stderr = io.StringIO()

    result = roundtable.main(
        [],
        cwd=home,
        home=home,
        stdin=TTYInput("9\n"),
        stderr=stderr,
        exec_runner=lambda *_: 0,
        chdir_runner=lambda _: None,
    )

    assert result == 2
    assert "pneu = a local mailroom for coding-agent seats" not in stderr.getvalue()
    assert "Choose a pneu project:" in stderr.getvalue()


def test_interactive_onboarding_ctrl_c_is_a_clean_cancellation(
    tmp_path, isolated_registry, fake_commands
):
    class InterruptingTTY:
        def isatty(self):
            return True

        def readline(self):
            raise KeyboardInterrupt

    stderr = io.StringIO()

    result = roundtable.main(
        [],
        cwd=tmp_path,
        home=tmp_path / "home",
        stdin=InterruptingTTY(),
        stderr=stderr,
        exec_runner=lambda *_: 0,
        chdir_runner=lambda _: None,
    )

    assert result == 130
    assert "cancelled by user (Ctrl-C)" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_onboarding_eof_after_project_selection_mentions_created_project(
    tmp_path, isolated_registry, fake_commands
):
    project = write_project(tmp_path / "project")
    register_project(project, isolated_registry)
    stderr = io.StringIO()

    result = roundtable.main(
        [],
        cwd=project,
        home=tmp_path / "home",
        stdin=TTYInput(""),
        stderr=stderr,
        exec_runner=lambda *_: 0,
        chdir_runner=lambda _: None,
    )

    assert result == 2
    assert f"project already created at {project}" in stderr.getvalue()


def test_onboarding_ctrl_c_after_project_selection_mentions_created_project(
    tmp_path, isolated_registry, fake_commands
):
    project = write_project(tmp_path / "project")
    register_project(project, isolated_registry)

    class InterruptingTTY:
        def isatty(self):
            return True

        def readline(self):
            raise KeyboardInterrupt

    stderr = io.StringIO()
    result = roundtable.main(
        [],
        cwd=project,
        home=tmp_path / "home",
        stdin=InterruptingTTY(),
        stderr=stderr,
        exec_runner=lambda *_: 0,
        chdir_runner=lambda _: None,
    )

    assert result == 130
    assert f"project already created at {project}" in stderr.getvalue()


def test_public_cli_has_no_pre_manifest_migration_surface():
    assert "migrate" not in roundtable.ALIASES
    assert "migrate" not in roundtable.HELP


def test_anchored_project_goes_directly_to_configured_seat_selector(
    tmp_path, isolated_registry, fake_commands
):
    project = write_project(
        tmp_path / "project",
        {
            "claude": ("claude-code", ["claude"]),
            "codex": ("codex", ["codex-a", "codex-b"]),
            "hermes": ("hermes-agent", ["hermes"]),
        },
    )
    register_project(project, isolated_registry)
    nested = project / "nested"
    nested.mkdir()
    stderr = io.StringIO()
    environment = {}
    exec_calls = []
    chdir_calls = []

    result = roundtable.main(
        [],
        cwd=nested,
        home=tmp_path / "home",
        stdin=TTYInput("3\n"),
        stderr=stderr,
        environ=environment,
        exec_runner=lambda path, argv: exec_calls.append((path, argv)) or 0,
        chdir_runner=chdir_calls.append,
    )

    assert result == 0
    assert f"pneu project: {project}" in stderr.getvalue()
    assert "Choose a pneu project:" not in stderr.getvalue()
    assert "not a pneu project yet" not in stderr.getvalue()
    assert "codex — codex-b" in stderr.getvalue()
    assert environment["RT_FROM"] == "codex-b"
    assert chdir_calls == [project]
    expected = fake_commands / "rt-codex"
    assert exec_calls == [(str(expected), [str(expected)])]


def test_anchored_unregistered_project_fails_before_seat_setup(
    tmp_path,
    isolated_registry,
    fake_commands,
):
    project = write_project(tmp_path / "project")
    stderr = io.StringIO()
    exec_calls = []

    result = roundtable.main(
        [],
        cwd=project,
        home=tmp_path / "home",
        stdin=TTYInput("1\n"),
        stderr=stderr,
        environ={},
        exec_runner=lambda *args: exec_calls.append(args),
        chdir_runner=lambda _: None,
    )

    assert result == 2
    assert "project registration preflight failed" in stderr.getvalue()
    assert exec_calls == []


def test_onboarding_can_safely_set_up_current_folder_without_git(
    tmp_path, isolated_registry, fake_commands
):
    folder = tmp_path / "existing"
    folder.mkdir()
    (folder / "README.md").write_text("# User file\n")
    init_calls = []

    def fake_init(command, cwd, check):
        init_calls.append((command, cwd, check))
        assert "--git" not in command
        write_project(cwd)
        register_project(cwd, isolated_registry)
        return SimpleNamespace(returncode=0)

    environment = {}
    result = roundtable.main(
        [],
        cwd=folder,
        home=tmp_path / "home",
        stdin=TTYInput("1\n\n1\n"),
        stderr=io.StringIO(),
        environ=environment,
        init_runner=fake_init,
        exec_runner=lambda *_: 0,
        chdir_runner=lambda _: None,
    )

    assert result == 0
    assert init_calls == [
        (
            [str(fake_commands / "roundtable-init"), "--here"],
            folder.resolve(),
            False,
        )
    ]
    assert (folder / "README.md").read_text() == "# User file\n"
    assert environment["RT_FROM"] == "codex"


def test_onboarding_can_set_up_another_existing_folder(
    tmp_path, isolated_registry, fake_commands
):
    cwd = tmp_path / "start"
    other = tmp_path / "other"
    cwd.mkdir()
    other.mkdir()
    init_calls = []

    def fake_init(command, cwd, check):
        init_calls.append((command, cwd, check))
        write_project(cwd)
        register_project(cwd, isolated_registry)
        return SimpleNamespace(returncode=0)

    result = roundtable.main(
        [],
        cwd=cwd,
        home=tmp_path / "home",
        stdin=TTYInput(f"2\n{other}\n\n1\n"),
        stderr=io.StringIO(),
        environ={},
        init_runner=fake_init,
        exec_runner=lambda *_: 0,
        chdir_runner=lambda _: None,
    )

    assert result == 0
    assert init_calls[0][0] == [
        str(fake_commands / "roundtable-init"),
        "--here",
    ]
    assert init_calls[0][1] == other.resolve()


def test_onboarding_creates_new_folder_and_only_passes_git_after_yes(
    tmp_path, isolated_registry, fake_commands
):
    cwd = tmp_path / "start"
    cwd.mkdir()
    init_calls = []

    def fake_init(command, cwd, check):
        init_calls.append((command, cwd, check))
        parent = Path(command[command.index("--parent") + 1])
        project = write_project(parent / command[1])
        register_project(project, isolated_registry)
        return SimpleNamespace(returncode=0)

    result = roundtable.main(
        [],
        cwd=cwd,
        home=tmp_path / "home",
        stdin=TTYInput("3\n\nnew-project\nyes\n1\n"),
        stderr=io.StringIO(),
        environ={},
        init_runner=fake_init,
        exec_runner=lambda *_: 0,
        chdir_runner=lambda _: None,
    )

    assert result == 0
    assert init_calls == [
        (
            [
                str(fake_commands / "roundtable-init"),
                "new-project",
                "--parent",
                str(cwd.resolve()),
                "--git",
            ],
            cwd.resolve(),
            False,
        )
    ]


def test_home_is_never_offered_as_the_current_project(
    tmp_path, isolated_registry, fake_commands
):
    home = tmp_path / "home"
    home.mkdir()
    stderr = io.StringIO()

    result = roundtable.main(
        [],
        cwd=home,
        home=home,
        stdin=TTYInput("9\n"),
        stderr=stderr,
        environ={},
        exec_runner=lambda *_: 0,
        chdir_runner=lambda _: None,
    )

    assert result == 2
    assert "Set up this folder safely" not in stderr.getvalue()
    assert "Set up another existing folder" in stderr.getvalue()
    assert "Create a new folder" in stderr.getvalue()


def test_existing_folder_selector_reports_symlink_loop_without_traceback(
    tmp_path,
) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to("loop", target_is_directory=True)

    with pytest.raises(roundtable.OnboardingError, match="cannot resolve project folder"):
        roundtable.canonical_existing_folder("loop/child", tmp_path)


def test_zero_is_not_accepted_as_a_menu_selection(
    tmp_path, isolated_registry, fake_commands
):
    folder = tmp_path / "folder"
    folder.mkdir()
    stderr = io.StringIO()

    result = roundtable.main(
        [],
        cwd=folder,
        home=tmp_path / "home",
        stdin=TTYInput("0\n"),
        stderr=stderr,
        environ={},
        exec_runner=lambda *_: 0,
        chdir_runner=lambda _: None,
    )

    assert result == 2
    assert "invalid project selection" in stderr.getvalue()


def test_registered_project_can_be_selected_without_reinitializing(
    tmp_path, isolated_registry, fake_commands
):
    project = write_project(tmp_path / "registered")
    register_project(
        project,
        isolated_registry,
        registered_at="2026-07-19T00:00:00Z",
    )
    cwd = tmp_path / "outside"
    cwd.mkdir()
    init_calls = []

    result = roundtable.main(
        [],
        cwd=cwd,
        home=tmp_path / "home",
        stdin=TTYInput("1\n1\n1\n"),
        stderr=io.StringIO(),
        environ={},
        init_runner=lambda *args, **kwargs: init_calls.append((args, kwargs)),
        exec_runner=lambda *_: 0,
        chdir_runner=lambda _: None,
    )

    assert result == 0
    assert init_calls == []


def test_registered_projects_are_grouped_in_a_second_level_menu(
    tmp_path, isolated_registry
):
    first = write_project(tmp_path / "first")
    second = write_project(tmp_path / "second")
    register_project(
        first,
        isolated_registry,
        registered_at="2026-07-19T00:00:00Z",
    )
    register_project(
        second,
        isolated_registry,
        registered_at="2026-07-20T00:00:00Z",
    )
    cwd = tmp_path / "outside"
    cwd.mkdir()
    stderr = io.StringIO()

    selected = roundtable.choose_project(
        cwd=cwd,
        home=tmp_path / "home",
        stdin=TTYInput("1\n2\n"),
        stderr=stderr,
    )

    assert selected == second
    output = stderr.getvalue()
    first_level, second_level = output.split("Select project: ", 1)
    assert first_level.count("Choose an existing project") == 1
    assert str(first) not in first_level
    assert str(second) not in first_level
    assert "Choose an existing pneu project:" in second_level
    assert second_level.index(str(first)) < second_level.index(str(second))


def test_registered_project_second_level_rejects_zero(
    tmp_path, isolated_registry
):
    project = write_project(tmp_path / "registered")
    register_project(
        project,
        isolated_registry,
        registered_at="2026-07-19T00:00:00Z",
    )
    cwd = tmp_path / "outside"
    cwd.mkdir()

    with pytest.raises(
        roundtable.OnboardingError,
        match="invalid existing project selection",
    ):
        roundtable.choose_project(
            cwd=cwd,
            home=tmp_path / "home",
            stdin=TTYInput("1\n0\n"),
            stderr=io.StringIO(),
        )


def test_installed_onboarding_previews_and_applies_selected_harness_once(
    tmp_path, isolated_registry, fake_commands
):
    project = write_project(tmp_path / "project")
    register_project(project, isolated_registry)
    prefix = tmp_path / "installed"
    prefix.mkdir()
    calls = []

    def fake_setup(command, **kwargs):
        calls.append((command, kwargs))
        subcommand = command[1]
        payload = {
            "ok": True,
            "command": subcommand,
            "harnesses": {
                "codex": {
                    "state": "not_configured" if subcommand == "status" else "planned",
                    "actions": ["merge ~/.codex/hooks.json"],
                }
            },
        }
        if subcommand == "apply":
            payload["harnesses"]["codex"]["state"] = "configured"
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    stderr = io.StringIO()
    environment = {"ROUNDTABLE_INSTALL_PREFIX": str(prefix)}
    exec_calls = []
    result = roundtable.main(
        [],
        cwd=project,
        home=tmp_path / "home",
        stdin=TTYInput("1\ny\n"),
        stderr=stderr,
        environ=environment,
        setup_runner=fake_setup,
        exec_runner=lambda path, argv: exec_calls.append((path, argv)) or 0,
        chdir_runner=lambda _: None,
    )

    assert result == 0
    assert [command[1] for command, _kwargs in calls] == [
        "status",
        "plan",
        "apply",
    ]
    assert all(kwargs["check"] is False for _command, kwargs in calls)
    assert "One-time codex integration setup" in stderr.getvalue()
    assert "pneu never bypasses hook trust" in stderr.getvalue()
    assert len(exec_calls) == 1


def test_installed_onboarding_decline_does_not_launch(
    tmp_path, isolated_registry, fake_commands
):
    project = write_project(tmp_path / "project")
    register_project(project, isolated_registry)
    prefix = tmp_path / "installed"
    prefix.mkdir()
    calls = []

    def fake_setup(command, **_kwargs):
        calls.append(command[1])
        state = "not_configured" if command[1] == "status" else "planned"
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "harnesses": {
                        "codex": {"state": state, "actions": ["merge hook"]}
                    },
                }
            ),
            stderr="",
        )

    stderr = io.StringIO()
    result = roundtable.main(
        [],
        cwd=project,
        home=tmp_path / "home",
        stdin=TTYInput("1\nn\n"),
        stderr=stderr,
        environ={"ROUNDTABLE_INSTALL_PREFIX": str(prefix)},
        setup_runner=fake_setup,
        exec_runner=lambda *_: pytest.fail("launcher must not run after setup decline"),
        chdir_runner=lambda _: None,
    )

    assert result == 2
    assert calls == ["status", "plan"]
    assert "nothing was launched" in stderr.getvalue()


def test_installed_direct_harness_command_runs_setup_before_launch(
    tmp_path, isolated_registry, fake_commands
):
    prefix = tmp_path / "installed"
    prefix.mkdir()
    calls = []

    def fake_setup(command, **_kwargs):
        calls.append(command[1])
        state = "configured" if command[1] in {"status", "apply"} else "planned"
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "harnesses": {
                        "codex": {
                            "state": state,
                            "actions": ["no changes"],
                        }
                    },
                }
            ),
            stderr="",
        )

    exec_calls = []
    result = roundtable.main(
        ["codex", "--example"],
        cwd=tmp_path,
        home=tmp_path / "home",
        stdin=TTYInput(""),
        stderr=io.StringIO(),
        environ={"ROUNDTABLE_INSTALL_PREFIX": str(prefix)},
        setup_runner=fake_setup,
        exec_runner=lambda path, argv: exec_calls.append((path, argv)) or 0,
    )

    assert result == 0
    assert calls == ["status"]
    expected = fake_commands / "rt-codex"
    assert exec_calls == [(str(expected), [str(expected), "--example"])]


def test_selector_marks_configured_but_missing_harness_unavailable(
    tmp_path, isolated_registry, fake_commands, monkeypatch
):
    project = write_project(
        tmp_path / "project",
        {
            "claude": ("claude-code", ["claude"]),
            "codex": ("codex", ["codex"]),
        },
    )

    def resolve(harness):
        if harness == "claude":
            raise roundtable.SelectionError("rt-claude: executable not found")
        return fake_commands / harness

    monkeypatch.setattr(roundtable, "harness_bin", resolve)
    stderr = io.StringIO()
    selected = roundtable.choose_seat(
        project,
        stdin=TTYInput("1\n"),
        stderr=stderr,
    )

    assert selected == ("codex", "codex")
    assert "unavailable: claude" in stderr.getvalue()
    assert (
        "1) codex — codex "
        "(starts with a visible automatic pneu activation turn)"
        in stderr.getvalue()
    )


@pytest.mark.parametrize(
    ("harness", "override"),
    [
        ("claude", "RT_CLAUDE_BIN"),
        ("codex", "RT_CODEX_BIN"),
        ("hermes", "RT_HERMES_BIN"),
        ("openclaw", "RT_OPENCLAW_BIN"),
        ("grok", "RT_GROK_BIN"),
    ],
)
def test_harness_detection_accepts_present_executable_and_rejects_broken_symlink(
    tmp_path, monkeypatch, harness, override
):
    executable = tmp_path / harness
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv(override, str(executable))

    assert roundtable.harness_bin(harness) == executable.resolve()

    executable.unlink()
    executable.symlink_to(tmp_path / ("missing-" + harness))
    with pytest.raises(roundtable.SelectionError):
        roundtable.harness_bin(harness)


def test_selector_shows_all_five_harnesses_and_plain_install_remedies(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project", {"claude": ("claude-code", ["claude"])})
    monkeypatch.setattr(
        roundtable,
        "harness_bin",
        lambda harness: (_ for _ in ()).throw(
            roundtable.SelectionError(f"rt-{harness}: executable not found")
        ),
    )
    stderr = io.StringIO()

    with pytest.raises(roundtable.OnboardingError, match="no launchable"):
        roundtable.choose_seat(project, stdin=TTYInput(""), stderr=stderr)

    rendered = stderr.getvalue()
    for harness in ("claude", "codex", "hermes", "openclaw", "grok"):
        assert f"unavailable: {harness}" in rendered
    assert "missing executable `openclaw`" in rendered
    assert "set RT_OPENCLAW_BIN" in rendered


def test_pty_single_card_enters_through_last_used_seat(
    tmp_path, isolated_registry, fake_commands, monkeypatch
):
    project = write_project(
        tmp_path / "project",
        {
            "claude": ("claude-code", ["claude"]),
            "codex": ("codex", ["codex"]),
        },
    )
    register_project(project, isolated_registry)
    launcher_state = project / ".roundtable" / "launcher.json"
    launcher_state.write_text(
        json.dumps(
            {
                "schema": roundtable.LAUNCHER_STATE_SCHEMA,
                "welcomePending": False,
                "lastSeat": "codex:codex",
            }
        )
    )
    monkeypatch.setattr(roundtable, "_active_worktree_count", lambda _root: 2)
    monkeypatch.setattr(
        roundtable,
        "_unread_by_seat",
        lambda _root, seats: [(agent, 0) for _harness, agent in seats],
    )
    monkeypatch.setattr(roundtable, "_phone_access_on", lambda _root: False)

    selected, rendered = run_with_pty(
        lambda stdin, stderr: roundtable.choose_seat_card(
            project, stdin=stdin, stderr=stderr
        ),
        b"\n",
    )

    assert selected == ("codex", "codex")
    assert "> Codex — codex" in rendered
    assert "active worktrees: 2" in rendered
    assert "unread mail: claude=0 codex=0" in rendered
    assert "phone access: off  [p]" in rendered
    assert "Enter launch · p phone access · w worktrees · ? guide · q quit" in rendered


def test_pty_card_phone_toggle_redraws_in_place(
    tmp_path, isolated_registry, fake_commands, monkeypatch
):
    project = write_project(
        tmp_path / "project",
        {"claude": ("claude-code", ["claude"])},
    )
    register_project(project, isolated_registry)
    state = {"enabled": False}
    monkeypatch.setattr(roundtable, "_phone_access_on", lambda _root: state["enabled"])

    def toggle(_root):
        state["enabled"] = True
        return "phone access enabled"

    monkeypatch.setattr(roundtable, "_toggle_phone_access", toggle)
    selected, rendered = run_with_pty(
        lambda stdin, stderr: roundtable.choose_seat_card(
            project, stdin=stdin, stderr=stderr
        ),
        b"p\n",
    )

    assert selected == ("claude", "claude")
    assert "phone access: off  [p]" in rendered
    assert "phone access: on  [p]" in rendered
    assert rendered.count("\x1b[2J\x1b[H") >= 2


def test_pty_first_run_welcome_single_enter_skips_both_offers(
    tmp_path, isolated_registry, fake_commands
):
    project = write_project(
        tmp_path / "project",
        {"claude": ("claude-code", ["claude"])},
    )
    register_project(project, isolated_registry)
    launcher_state = project / ".roundtable" / "launcher.json"
    launcher_state.write_text(
        json.dumps(
            {
                "schema": roundtable.LAUNCHER_STATE_SCHEMA,
                "welcomePending": True,
                "lastSeat": None,
            }
        )
    )

    continued, rendered = run_with_pty(
        lambda stdin, stderr: roundtable.show_first_run_welcome(
            project, stdin=stdin, stderr=stderr
        ),
        b"\n",
    )

    assert continued is True
    assert "Enter continue · ? guide · p phone access · q quit" in rendered
    assert "only Claude mobile/web remote sessions for this project" in rendered
    assert "desktop seats and other harnesses are untouched" in rendered
    assert json.loads(launcher_state.read_text())["welcomePending"] is False


def test_non_tty_output_keeps_numbered_selector_fallback(
    tmp_path, isolated_registry, fake_commands
):
    project = write_project(tmp_path / "project")
    register_project(project, isolated_registry)
    stdin = TTYInput("1\n")
    stderr = io.StringIO()

    assert not roundtable._rich_card_available(stdin, stderr)
    selected = roundtable.choose_seat(project, stdin=stdin, stderr=stderr)

    assert selected == ("codex", "codex")
    assert "1) codex — codex" in stderr.getvalue()
    assert "\x1b[2J" not in stderr.getvalue()


def test_direct_missing_harness_fails_before_setup(
    tmp_path, isolated_registry, fake_commands, monkeypatch
):
    monkeypatch.setattr(
        roundtable,
        "harness_bin",
        lambda _harness: (_ for _ in ()).throw(
            roundtable.SelectionError("rt-claude: executable not found")
        ),
    )
    setup_calls = []
    stderr = io.StringIO()

    result = roundtable.main(
        ["claude"],
        cwd=tmp_path,
        home=tmp_path / "home",
        stdin=TTYInput(""),
        stderr=stderr,
        environ={"ROUNDTABLE_INSTALL_PREFIX": str(tmp_path / "prefix")},
        setup_runner=lambda *args, **kwargs: setup_calls.append((args, kwargs)),
        exec_runner=lambda *_: pytest.fail("missing harness must not launch"),
    )

    assert result == 2
    assert setup_calls == []
    assert "executable not found" in stderr.getvalue()


def test_first_project_onboarding_explains_non_git_topology(
    tmp_path, isolated_registry, fake_commands
):
    folder = tmp_path / "folder"
    folder.mkdir()
    stderr = io.StringIO()

    result = roundtable.main(
        [],
        cwd=folder,
        home=tmp_path / "home",
        stdin=TTYInput("0\n"),
        stderr=stderr,
        environ={},
        exec_runner=lambda *_: 0,
        chdir_runner=lambda _: None,
    )

    assert result == 2
    output = stderr.getvalue()
    assert f"This folder is not a pneu project yet: {folder}" in output
    assert "[durable mailboxes]" in output
    assert "Git is optional" in output


def test_unavailable_detail_distinguishes_broken_from_absent(tmp_path, monkeypatch):
    """A broken/non-executable binary names its path; true absence says not installed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.delenv("RT_HERMES_BIN", raising=False)
    (tmp_path / "bin").mkdir()

    absent = roundtable.harness_unavailable_detail("hermes", "executable not found")
    assert "not installed" in absent
    assert "RT_HERMES_BIN" in absent

    broken = tmp_path / "bin" / "hermes"
    broken.symlink_to(tmp_path / "missing-hermes-target")
    present = roundtable.harness_unavailable_detail("hermes", "executable not found")
    assert "missing or not executable" in present
    assert str(broken) in present
