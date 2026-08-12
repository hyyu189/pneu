from __future__ import annotations

import io
import json
import os
import pty
import select
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "bin" / "rt-worktree"
sys.path.insert(0, str(ROOT / "bin"))

from _rtsurface import (  # noqa: E402
    SurfaceError,
    SurfaceSelection,
    detect_surface,
    launch_surface,
    launcher_shell_command,
)


def git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


@pytest.fixture
def lab(tmp_path, monkeypatch):
    base = tmp_path / "repo"
    base.mkdir()
    git(base, "init", "-q", "-b", "main")
    git(base, "config", "user.name", "Roundtable Tests")
    git(base, "config", "user.email", "roundtable@example.invalid")
    (base / "README.md").write_text("fixture\n")
    git(base, "add", "README.md")
    git(base, "commit", "-qm", "fixture")

    registry = tmp_path / "projects.yaml"
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("ROUNDTABLE_ONBOARDING_SUBPROCESS", "1")
    return tmp_path, base, registry, runtime


def run_tool(*args: str, cwd: Path, env: dict[str, str] | None = None):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=cwd,
        env=merged,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def run_pty(args: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, str(TOOL), *args],
        cwd=cwd,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    output = bytearray()
    answers = [b"maybe\n", b"y\n"]
    answer_index = 0
    deadline = time.monotonic() + 20
    try:
        while True:
            if time.monotonic() > deadline:
                process.kill()
                raise AssertionError(f"pty command timed out: {output!r}")
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    output.extend(chunk)
                    if b"Proceed? [y/N]:" in output and answer_index < len(answers):
                        os.write(master, answers[answer_index])
                        answer_index += 1
            if process.poll() is not None:
                break
        return process.returncode, output.decode(errors="replace")
    finally:
        os.close(master)


def surface_environment(fake_bin: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("HERDR_ENV", "HERDR_PANE_ID", "RT_SURFACE", "TMUX", "TMUX_PANE"):
        environment.pop(name, None)
    environment["PATH"] = os.pathsep.join(
        part for part in (str(fake_bin), os.environ.get("PATH", "")) if part
    )
    return environment


def fake_herdr(
    tmp_path: Path,
    *,
    width: int = 160,
    height: int = 40,
    fail_command: str = "",
) -> tuple[Path, Path, dict[str, str]]:
    fake_bin = tmp_path / "fake-herdr-bin"
    fake_bin.mkdir()
    trace = tmp_path / "herdr-calls.jsonl"
    executable = fake_bin / "herdr"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["RT_TEST_SURFACE_TRACE"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
command = " ".join(args[:2])
if os.environ.get("RT_TEST_SURFACE_FAIL") == command:
    print("simulated herdr failure", file=sys.stderr)
    raise SystemExit(7)
if args[:2] == ["pane", "layout"]:
    print(json.dumps({{"result": {{"layout": {{"width": {width}, "height": {height}}}}}}}))
elif args[:2] == ["pane", "split"]:
    print(json.dumps({{"first": "wrong", "result": {{"pane": {{"pane_id": "w1:p9"}}}}}}))
elif args[:2] == ["pane", "run"]:
    print("{{}}")
else:
    print("unexpected herdr command", file=sys.stderr)
    raise SystemExit(8)
"""
    )
    executable.chmod(0o755)
    environment = surface_environment(fake_bin)
    environment.update(
        {
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w1:p1",
            "RT_TEST_SURFACE_TRACE": str(trace),
            "RT_TEST_SURFACE_FAIL": fail_command,
        }
    )
    return executable, trace, environment


def fake_tmux(
    tmp_path: Path,
    *,
    fail_command: str = "",
) -> tuple[Path, Path, dict[str, str]]:
    fake_bin = tmp_path / "fake-tmux-bin"
    fake_bin.mkdir()
    trace = tmp_path / "tmux-calls.jsonl"
    executable = fake_bin / "tmux"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["RT_TEST_SURFACE_TRACE"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
if os.environ.get("RT_TEST_SURFACE_FAIL") == args[0]:
    print("simulated tmux failure", file=sys.stderr)
    raise SystemExit(9)
if args[0] == "list-clients":
    print("attached")
elif args[0] in {{"split-window", "new-window"}}:
    print("attached:4.2")
else:
    print("unexpected tmux command", file=sys.stderr)
    raise SystemExit(8)
"""
    )
    executable.chmod(0o755)
    environment = surface_environment(fake_bin)
    environment.update(
        {
            "RT_TEST_SURFACE_TRACE": str(trace),
            "RT_TEST_SURFACE_FAIL": fail_command,
        }
    )
    return executable, trace, environment


def read_calls(trace: Path) -> list[list[str]]:
    return [json.loads(line) for line in trace.read_text().splitlines()]


def create_demo(lab, *, agents: str | None = None) -> Path:
    tmp_path, base, _registry, _runtime = lab
    created = run_tool("add", "demo", "--repo", str(base), "--yes", cwd=base)
    assert created.returncode == 0, created.stderr
    target = tmp_path / "repo-worktree" / "demo"
    if agents is not None:
        (target / ".roundtable" / "agents.yaml").write_text(agents)
    return target


def test_non_git_cwd_error_names_repo_override(tmp_path, monkeypatch):
    monkeypatch.setenv("RT_PROJECTS_FILE", str(tmp_path / "projects.yaml"))
    result = run_tool("add", "demo", cwd=tmp_path)
    assert result.returncode == 2
    assert "not inside a Git repository" in result.stderr
    assert "--repo" in result.stderr


def test_dry_run_restates_without_mutating(lab):
    tmp_path, base, registry, _runtime = lab
    result = run_tool("add", "demo", "--repo", str(base), "--dry-run", cwd=base)
    assert result.returncode == 0, result.stderr
    assert "repo root:" in result.stdout
    assert "current branch@commit: main@" in result.stdout
    assert "derived group key: git:" in result.stdout
    assert "target path:" in result.stdout
    assert "new branch: wt/demo" in result.stdout
    assert "future address: codex@demo" in result.stdout
    assert "dry-run: no changes made" in result.stdout
    assert f"target path: {tmp_path / 'repo-worktree' / 'demo'}" in result.stdout
    assert not (tmp_path / "repo-worktree").exists()
    assert not registry.exists()


def test_pty_confirmation_retries_then_creates_default_container(lab):
    tmp_path, base, registry, runtime = lab
    env = os.environ.copy()
    env.update(
        {
            "RT_PROJECTS_FILE": str(registry),
            "RT_RUNTIME_DIR": str(runtime),
            "RT_CODEX_RUNTIME_DIR": str(runtime),
        }
    )
    returncode, output = run_pty(["add", "demo"], cwd=base, env=env)
    assert returncode == 0, output
    assert "please try again" in output
    assert "worktree added:" in output
    assert (
        tmp_path / "repo-worktree" / "demo" / ".roundtable" / "project.json"
    ).is_file()


def test_name_collision_is_checked_before_target_mutation(lab):
    tmp_path, base, registry, _runtime = lab
    first = run_tool("add", "demo", "--repo", str(base), "--yes", cwd=base)
    assert first.returncode == 0, first.stderr
    second_path = tmp_path / "other" / "demo"
    second_path.parent.mkdir()
    second = run_tool(
        "add",
        "demo",
        "--repo",
        str(base),
        "--path",
        str(second_path),
        "--yes",
        cwd=base,
    )
    assert second.returncode == 2
    assert "already exists in derived group" in second.stderr
    assert not second_path.exists()


def test_remove_refuses_active_unhealthy_owned_seat(lab):
    tmp_path, base, registry, runtime = lab
    created = run_tool("add", "demo", "--repo", str(base), "--yes", cwd=base)
    assert created.returncode == 0, created.stderr
    target = tmp_path / "repo-worktree" / "demo"

    sys.path.insert(0, str(ROOT / "bin"))
    from _rtruntime import claim, release

    token = claim(target, "codex", "codex", owner_pid=os.getpid())
    try:
        removed = run_tool("remove", "demo", "--yes", cwd=base)
        assert removed.returncode == 2
        assert "codex/codex=active_unhealthy" in removed.stderr
        assert target.exists()
    finally:
        assert release(token)


def test_remove_full_cycle_deletes_merged_branch(lab):
    tmp_path, base, registry, runtime = lab
    created = run_tool("add", "demo", "--repo", str(base), "--yes", cwd=base)
    assert created.returncode == 0, created.stderr
    target = tmp_path / "repo-worktree" / "demo"
    sys.path.insert(0, str(ROOT / "bin"))
    from _rtruntime import claim, release, seat_paths

    token = claim(target, "codex", "codex", owner_pid=os.getpid())
    assert release(token)
    runtime_project = seat_paths(target, "codex").project_dir
    assert runtime_project.exists()

    removed = run_tool("remove", "demo", "--yes", cwd=base)
    assert removed.returncode == 0, removed.stderr
    assert not target.exists()
    assert "registry: tombstoned" in removed.stdout
    assert f"runtime: reclaimed {runtime_project}" in removed.stdout
    assert not runtime_project.exists()
    assert "deleted (merged): wt/demo" in removed.stdout
    assert git(base, "show-ref", "--verify", "refs/heads/wt/demo", check=False).returncode != 0


def test_tombstoned_symlink_drift_does_not_block_worktree_add(lab):
    tmp_path, base, _registry, _runtime = lab
    created = run_tool("add", "retired", "--repo", str(base), "--yes", cwd=base)
    assert created.returncode == 0, created.stderr
    removed = run_tool("remove", "retired", "--yes", cwd=base)
    assert removed.returncode == 0, removed.stderr
    retired = tmp_path / "repo-worktree" / "retired"
    relocated = tmp_path / "relocated-retired"
    relocated.mkdir()
    retired.symlink_to(relocated, target_is_directory=True)

    added = run_tool("add", "next", "--repo", str(base), "--yes", cwd=base)

    assert added.returncode == 0, added.stderr
    assert (
        tmp_path / "repo-worktree" / "next" / ".roundtable" / "project.json"
    ).is_file()
    assert "tombstoned-row:" in added.stderr
    assert "registered path drifted" in added.stderr


def test_remove_keep_branch_retains_unmerged_branch(lab):
    tmp_path, base, registry, _runtime = lab
    created = run_tool("add", "demo", "--repo", str(base), "--yes", cwd=base)
    assert created.returncode == 0, created.stderr
    target = tmp_path / "repo-worktree" / "demo"
    (target / "README.md").write_text("unmerged change\n")
    git(target, "add", "README.md")
    git(target, "commit", "-qm", "unmerged")

    removed = run_tool(
        "remove",
        "demo",
        "--keep-branch",
        "--yes",
        cwd=base,
    )
    assert removed.returncode == 0, removed.stderr
    assert not target.exists()
    assert "preserved (--keep-branch): wt/demo" in removed.stdout
    assert git(base, "show-ref", "--verify", "refs/heads/wt/demo", check=False).returncode == 0


def test_target_inside_repo_is_rejected_before_git_mutation(lab):
    _tmp_path, base, _registry, _runtime = lab
    target = base / "nested" / "demo"
    target.parent.mkdir()
    result = run_tool(
        "add",
        "demo",
        "--repo",
        str(base),
        "--path",
        str(target),
        "--yes",
        cwd=base,
    )
    assert result.returncode == 2
    assert "sibling outside" in result.stderr
    assert not target.exists()


def test_surface_detection_flag_and_environment_precede_ambient():
    ambient = {
        "RT_SURFACE": "tmux",
        "HERDR_ENV": "1",
        "TMUX": "inside",
        "PATH": "",
    }
    explicit = detect_surface("print", environ=ambient)
    assert explicit == SurfaceSelection("print", "--surface")

    configured = detect_surface(
        None,
        environ={**ambient, "RT_SURFACE": "print"},
    )
    assert configured == SurfaceSelection("print", "RT_SURFACE")


def test_surface_detection_ambient_herdr_requires_exact_one():
    exact = detect_surface(None, environ={"HERDR_ENV": "1", "PATH": ""})
    assert exact == SurfaceSelection("herdr", "HERDR_ENV=1")

    not_exact = detect_surface(
        None,
        environ={"HERDR_ENV": "true", "PATH": ""},
    )
    assert not_exact == SurfaceSelection("print", "fallback")


def test_surface_detection_uses_reachable_tmux_with_attached_client(tmp_path):
    executable, trace, environment = fake_tmux(tmp_path)

    selected = detect_surface(None, environ=environment)

    assert selected == SurfaceSelection(
        "tmux",
        "attached tmux client",
        executable=executable.resolve(),
        tmux_session="attached",
    )
    assert read_calls(trace) == [
        ["list-clients", "-F", "#{client_session}"],
    ]


@pytest.mark.parametrize(
    ("explicit", "environment", "source"),
    [
        ("warp", {}, "--surface"),
        (None, {"RT_SURFACE": "warp"}, "RT_SURFACE"),
    ],
)
def test_unknown_surface_is_a_hard_error(explicit, environment, source):
    with pytest.raises(SurfaceError, match=source):
        detect_surface(explicit, environ={**environment, "PATH": ""})


@pytest.mark.parametrize(
    ("width", "height", "direction"),
    [(160, 40, "right"), (60, 50, "down")],
)
def test_herdr_backend_splits_runs_and_parses_nested_pane_id(
    tmp_path,
    width,
    height,
    direction,
):
    executable, trace, environment = fake_herdr(
        tmp_path,
        width=width,
        height=height,
    )
    tree = tmp_path / "tree with spaces"
    tree.mkdir()
    launcher = tmp_path / "installed" / "rt-codex"
    launcher.parent.mkdir()
    launcher.write_text("fixture\n")

    result = launch_surface(
        SurfaceSelection("herdr", "test", executable=executable),
        tree=tree,
        launcher=launcher,
        agent_id="codex-review",
        environ=environment,
        stdout=io.StringIO(),
    )

    assert result.launched is True
    assert result.surface == {"kind": "herdr", "pane": "w1:p9"}
    calls = read_calls(trace)
    assert calls[0] == ["pane", "layout", "--pane", "w1:p1"]
    assert calls[1] == [
        "pane",
        "split",
        "--current",
        "--direction",
        direction,
        "--cwd",
        str(tree),
        "--no-focus",
    ]
    assert calls[2] == [
        "pane",
        "run",
        "w1:p9",
        launcher_shell_command(launcher, "codex-review"),
    ]


def test_explicit_herdr_surface_still_requires_herdr_gate(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    launcher = tmp_path / "rt-codex"
    launcher.write_text("fixture\n")
    with pytest.raises(SurfaceError, match="HERDR_ENV=1"):
        launch_surface(
            SurfaceSelection("herdr", "--surface"),
            tree=tree,
            launcher=launcher,
            agent_id="codex",
            environ={"PATH": ""},
            stdout=io.StringIO(),
        )


@pytest.mark.parametrize("inside", [True, False])
def test_tmux_backend_uses_current_split_or_attached_new_window(tmp_path, inside):
    executable, trace, environment = fake_tmux(tmp_path)
    if inside:
        environment["TMUX"] = "socket,server,0"
    tree = tmp_path / "tree"
    tree.mkdir()
    launcher = tmp_path / "rt-claude"
    launcher.write_text("fixture\n")

    result = launch_surface(
        SurfaceSelection("tmux", "test", executable=executable),
        tree=tree,
        launcher=launcher,
        agent_id="claude",
        environ=environment,
        stdout=io.StringIO(),
    )

    assert result.surface == {"kind": "tmux", "target": "attached:4.2"}
    calls = read_calls(trace)
    launch_call = calls[-1]
    if inside:
        assert len(calls) == 1
        assert launch_call[0] == "split-window"
    else:
        assert calls[0] == ["list-clients", "-F", "#{client_session}"]
        assert launch_call[0] == "new-window"
        assert launch_call[launch_call.index("-t") + 1] == "attached"
    assert launch_call[launch_call.index("-c") + 1] == str(tree)
    assert launch_call[-1] == launcher_shell_command(launcher, "claude")


def test_tmux_backend_failure_names_surface_and_attempted_command(tmp_path):
    executable, _trace, environment = fake_tmux(
        tmp_path,
        fail_command="split-window",
    )
    environment["TMUX"] = "socket,server,0"
    tree = tmp_path / "tree"
    tree.mkdir()
    launcher = tmp_path / "rt-codex"
    launcher.write_text("fixture\n")

    with pytest.raises(SurfaceError) as captured:
        launch_surface(
            SurfaceSelection("tmux", "test", executable=executable),
            tree=tree,
            launcher=launcher,
            agent_id="codex",
            environ=environment,
            stdout=io.StringIO(),
        )

    assert "tmux surface command failed" in str(captured.value)
    assert "split-window" in str(captured.value)


def test_print_fallback_emits_exact_command_and_distinct_status(tmp_path):
    tree = tmp_path / "tree with spaces"
    tree.mkdir()
    launcher = tmp_path / "installed" / "rt-codex"
    launcher.parent.mkdir()
    launcher.write_text("fixture\n")
    stdout = io.StringIO()

    result = launch_surface(
        SurfaceSelection("print", "fallback"),
        tree=tree,
        launcher=launcher,
        agent_id="codex",
        environ={},
        stdout=stdout,
    )

    expected = (
        f"cd {shlex.quote(str(tree))} && "
        f"{launcher_shell_command(launcher, 'codex')}"
    )
    assert result.launched is False
    assert result.command == expected
    assert stdout.getvalue() == (
        "status: not launched, printed\n"
        f"command: {expected}\n"
    )


def test_open_requires_seat_when_worktree_configures_several(lab):
    _tmp_path, base, _registry, _runtime = lab
    create_demo(lab)

    result = run_tool("open", "demo", "--surface", "print", cwd=base)

    assert result.returncode == 2
    assert "multiple seats are configured" in result.stderr
    assert "pass --seat AGENT" in result.stderr
    assert "not launched, printed" not in result.stdout


def test_open_records_herdr_surface_only_after_success(lab, tmp_path):
    _lab_tmp, base, _registry, runtime = lab
    target = create_demo(
        lab,
        agents=(
            "schema: roundtable.agents.v1\n"
            "project: .\n"
            "agents:\n"
            "  codex:\n"
            "    harness: codex\n"
            "    instances:\n"
            "      - id: codex\n"
        ),
    )
    _executable, trace, environment = fake_herdr(tmp_path)
    environment.update(
        {
            "RT_PROJECTS_FILE": str(_registry),
            "RT_RUNTIME_DIR": str(runtime),
            "RT_CODEX_RUNTIME_DIR": str(runtime),
        }
    )

    result = run_tool(
        "open",
        "demo",
        "--surface",
        "herdr",
        cwd=base,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert "surface: herdr:w1:p9" in result.stdout
    sys.path.insert(0, str(ROOT / "bin"))
    from _rtruntime import seat_paths

    record_path = seat_paths(target, "codex", root=runtime).surface
    payload = json.loads(record_path.read_text())
    assert payload["agentId"] == "codex"
    assert payload["harness"] == "codex"
    assert payload["surface"] == {"kind": "herdr", "pane": "w1:p9"}
    assert read_calls(trace)[-1][:3] == ["pane", "run", "w1:p9"]


def test_failed_backend_names_command_and_writes_no_surface_record(lab, tmp_path):
    _lab_tmp, base, _registry, runtime = lab
    target = create_demo(
        lab,
        agents=(
            "schema: roundtable.agents.v1\n"
            "project: .\n"
            "agents:\n"
            "  codex:\n"
            "    harness: codex\n"
        ),
    )
    _executable, _trace, environment = fake_herdr(
        tmp_path,
        fail_command="pane run",
    )
    environment.update(
        {
            "RT_PROJECTS_FILE": str(_registry),
            "RT_RUNTIME_DIR": str(runtime),
            "RT_CODEX_RUNTIME_DIR": str(runtime),
        }
    )

    result = run_tool(
        "open",
        "demo",
        "--surface",
        "herdr",
        cwd=base,
        env=environment,
    )

    assert result.returncode == 2
    assert "herdr surface command failed" in result.stderr
    assert "pane run w1:p9" in result.stderr
    from _rtruntime import seat_paths

    assert not seat_paths(target, "codex", root=runtime).surface.exists()
