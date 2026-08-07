from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "bin" / "rt-worktree"


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
    assert not (tmp_path / "demo").exists()
    assert not registry.exists()


def test_pty_confirmation_retries_then_creates_default_sibling(lab):
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
    assert (tmp_path / "demo" / ".roundtable" / "project.json").is_file()


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
    target = tmp_path / "demo"

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
    target = tmp_path / "demo"
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


def test_remove_keep_branch_retains_unmerged_branch(lab):
    tmp_path, base, registry, _runtime = lab
    created = run_tool("add", "demo", "--repo", str(base), "--yes", cwd=base)
    assert created.returncode == 0, created.stderr
    target = tmp_path / "demo"
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
