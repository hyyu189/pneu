import os
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "bin" / "roundtable-init"


def run_init(tmp_path, *args, cwd=None, env_extra=None, umask=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "RT_PROJECTS_FILE": str(tmp_path / "projects.yaml"),
        }
    )
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(INIT), *args],
        cwd=cwd or tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        preexec_fn=(lambda: os.umask(umask)) if umask is not None else None,
    )


def test_new_project_defaults_to_no_git(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()

    result = run_init(tmp_path, "plain", "--parent", str(parent))

    project = parent / "plain"
    assert result.returncode == 0, result.stderr
    assert (project / ".roundtable" / "agents.yaml").is_file()
    assert (project / ".claude" / "skills").is_symlink()
    assert os.readlink(project / ".claude" / "skills") == "../skills"
    grok = (project / "GROK.md").read_text()
    assert 'role:  # optional — e.g. "implementation and tests" — assign per project' in grok
    assert "rt-inbox --fenced --archive-quiet-acks -f json" in grok
    assert "After resuming a session" in grok
    assert not (project / ".git").exists()
    assert "git: not initialized (use --git to opt in)" in result.stdout


def test_launcher_owned_init_suppresses_interactive_next_steps(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()

    result = run_init(
        tmp_path,
        "nested",
        "--parent",
        str(parent),
        env_extra={"ROUNDTABLE_ONBOARDING_SUBPROCESS": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert "next:" not in result.stdout


def test_new_project_initializes_git_only_with_explicit_flag(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()

    result = run_init(tmp_path, "versioned", "--parent", str(parent), "--git")

    project = parent / "versioned"
    log = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (project / ".git").is_dir()
    assert log.returncode == 0, log.stderr
    assert log.stdout.splitlines() == [
        "Initial: versioned bootstrapped via roundtable-init"
    ]


def test_new_project_is_guard_safe_under_group_writable_umask(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()

    result = run_init(
        tmp_path,
        "guarded",
        "--parent",
        str(parent),
        "--git",
        umask=0o002,
    )

    project = parent / "guarded"
    guarded_paths = [
        project,
        project / ".roundtable",
        project / ".roundtable" / "agents.yaml",
        project / ".roundtable" / ".gitignore",
        project / ".roundtable" / "project.json",
    ]
    assert result.returncode == 0, result.stderr
    for path in guarded_paths:
        assert path.exists()
        assert path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH) == 0


def test_git_and_no_git_are_mutually_exclusive(tmp_path):
    result = run_init(tmp_path, "invalid", "--git", "--no-git")

    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr
    assert not (tmp_path / "invalid").exists()


def test_here_preserves_user_files_and_marked_appends_are_idempotent(tmp_path):
    project = tmp_path / "existing work"
    project.mkdir()
    originals = {
        "AGENTS.md": "# My agent rules\n\nKeep this first.\n",
        "README.md": "# My notes\n\nDo not replace me.\n",
        ".gitignore": "private-output/\n",
    }
    for rel, content in originals.items():
        (project / rel).write_text(content)

    first = run_init(tmp_path, "--here", cwd=project)
    snapshots = {
        rel: (project / rel).read_text()
        for rel in originals
    }
    second = run_init(tmp_path, "--here", cwd=project)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "configured" in first.stdout
    assert "already configured" in second.stdout
    assert (project / ".roundtable" / "agents.yaml").is_file()
    assert not (project / ".git").exists()
    for rel, original in originals.items():
        content = (project / rel).read_text()
        assert content.startswith(original)
        assert content == snapshots[rel]
        assert content.count("BEGIN Roundtable") == 1
        assert content.count("END Roundtable") == 1


def test_here_preserves_user_grok_orientation_and_adds_rearm_contract_once(tmp_path):
    project = tmp_path / "existing-grok-project"
    project.mkdir()
    original = "# My Grok rules\n\nKeep this first.\n"
    (project / "GROK.md").write_text(original)

    first = run_init(tmp_path, "--here", cwd=project)
    snapshot = (project / "GROK.md").read_text()
    second = run_init(tmp_path, "--here", cwd=project)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert snapshot.startswith(original)
    assert (project / "GROK.md").read_text() == snapshot
    assert snapshot.count("BEGIN Roundtable") == 1
    assert "re-arm one persistent mailbox monitor" in snapshot


def test_here_writes_a_portable_project_reference_for_yaml_sensitive_path(tmp_path):
    project = tmp_path / "existing ${date} # notes"
    project.mkdir()

    result = run_init(tmp_path, "--here", cwd=project)
    document = yaml.safe_load(
        (project / ".roundtable" / "agents.yaml").read_text()
    )

    assert result.returncode == 0, result.stderr
    assert document["project"] == "."
    assert (project / "README.md").read_text().startswith(
        "# existing ${date} # notes\n"
    )


def test_git_commit_is_portable_and_tracks_relative_claude_link(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()

    result = run_init(tmp_path, "portable", "--parent", str(parent), "--git")

    project = parent / "portable"
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert tracked.returncode == 0, tracked.stderr
    tracked_paths = tracked.stdout.splitlines()
    assert ".roundtable/agents.yaml" in tracked_paths
    assert ".roundtable/.gitignore" in tracked_paths
    assert ".claude/skills" in tracked_paths
    assert (project / ".claude" / "skills").is_symlink()
    assert os.readlink(project / ".claude" / "skills") == "../skills"
    assert status.returncode == 0, status.stderr
    assert status.stdout == ""

    tracked_text = "\n".join(
        (project / path).read_text()
        for path in tracked_paths
        if (project / path).is_file() and not (project / path).is_symlink()
    )
    assert str(project.resolve()) not in tracked_text
    assert str(tmp_path.resolve()) not in tracked_text


def test_here_preserves_user_managed_claude_skills_directory_unignored(tmp_path):
    project = tmp_path / "existing-project"
    user_skill = project / ".claude" / "skills" / "private" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("user managed\n")
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=project,
        check=True,
    )

    result = run_init(tmp_path, "--here", cwd=project)
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".claude/skills/private/SKILL.md"],
        cwd=project,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert user_skill.read_text() == "user managed\n"
    assert (project / ".claude" / "skills").is_dir()
    assert not (project / ".claude" / "skills").is_symlink()
    assert ".claude/skills" not in (project / ".gitignore").read_text().splitlines()
    assert ignored.returncode == 1


def test_here_recognizes_an_existing_generated_project_from_an_earlier_date(tmp_path):
    project = tmp_path / "generated"
    created = run_init(tmp_path, "generated", "--parent", str(tmp_path))
    assert created.returncode == 0, created.stderr
    readme = project / "README.md"
    original = readme.read_text()
    dated = original.replace(date.today().isoformat(), "2000-01-02")
    readme.write_text(dated)

    repeated = run_init(tmp_path, "--here", cwd=project)

    assert repeated.returncode == 0, repeated.stderr
    assert "already configured" in repeated.stdout
    assert readme.read_text() == dated
    assert "BEGIN Roundtable" not in dated


def test_here_git_flag_does_not_commit_inside_existing_repository(tmp_path):
    project = tmp_path / "repository"
    project.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=project,
        check=True,
    )
    (project / "user-file.txt").write_text("keep me uncommitted\n")

    result = run_init(tmp_path, "--here", "--git", cwd=project)
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    log = subprocess.run(
        ["git", "rev-list", "--all", "--count"],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "existing repository preserved (no init or commit)" in result.stdout
    assert status.returncode == 0, status.stderr
    assert "user-file.txt" in status.stdout
    assert ".roundtable/" in status.stdout
    assert log.returncode == 0, log.stderr
    assert log.stdout.strip() == "0"


def test_git_routing_environment_cannot_redirect_initialization(tmp_path):
    project = tmp_path / "target"
    project.mkdir()
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=decoy,
        check=True,
    )

    result = run_init(
        tmp_path,
        "--here",
        "--git",
        cwd=project,
        env_extra={
            "GIT_DIR": str(decoy / ".git"),
            "GIT_WORK_TREE": str(project),
            "GIT_INDEX_FILE": str(decoy / ".git" / "index"),
        },
    )
    target_top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    decoy_count = subprocess.run(
        ["git", "rev-list", "--all", "--count"],
        cwd=decoy,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "git: initialized with an initial commit" in result.stdout
    assert target_top.returncode == 0, target_top.stderr
    assert Path(target_top.stdout.strip()) == project.resolve()
    assert decoy_count.stdout.strip() == "0"


def test_here_preflight_conflict_leaves_directory_untouched(tmp_path):
    project = tmp_path / "conflicted"
    project.mkdir()
    (project / "AGENTS.md").write_text("user-owned\n")
    (project / "README.md").mkdir()
    before = sorted(path.relative_to(project) for path in project.rglob("*"))

    result = run_init(tmp_path, "--here", cwd=project)

    after = sorted(path.relative_to(project) for path in project.rglob("*"))
    assert result.returncode != 0
    assert "expected a regular file" in result.stderr
    assert before == after
    assert (project / "AGENTS.md").read_text() == "user-owned\n"
    assert not (project / ".roundtable").exists()


def test_here_group_writable_root_fails_before_mutation_with_remedy(tmp_path):
    project = tmp_path / "shared"
    project.mkdir(mode=0o775)
    project.chmod(0o775)
    user_file = project / "notes.txt"
    user_file.write_text("unchanged\n")

    result = run_init(tmp_path, "--here", cwd=project)

    assert result.returncode != 0
    assert "group/other writable" in result.stderr
    assert f"chmod go-w {project}" in result.stderr
    assert user_file.read_text() == "unchanged\n"
    assert not (project / ".roundtable").exists()


def test_here_preflight_rejects_foreign_symlink_without_writes(tmp_path):
    project = tmp_path / "linked"
    project.mkdir()
    source = tmp_path / "outside-readme"
    source.write_text("outside\n")
    (project / "README.md").symlink_to(source)

    result = run_init(tmp_path, "--here", cwd=project)

    assert result.returncode != 0
    assert "refusing symbolic-link file" in result.stderr
    assert source.read_text() == "outside\n"
    assert not (project / ".roundtable").exists()


def test_here_rejects_file_at_claude_project_skills_path_without_writes(
    tmp_path,
):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    collision = project / ".claude" / "skills"
    collision.write_text("user file\n")

    result = run_init(tmp_path, "--here", cwd=project)

    assert result.returncode != 0
    assert "expected a directory" in result.stderr
    assert collision.read_text() == "user file\n"
    assert not (project / ".roundtable").exists()


def test_here_never_turns_the_home_directory_into_a_project(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    result = run_init(tmp_path, "--here", cwd=home)

    assert result.returncode != 0
    assert "refusing to use the home" in result.stderr
    assert not (home / ".roundtable").exists()


def test_here_skips_marker_appends_in_linked_worktree(tmp_path):
    def git(*arguments, cwd):
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
            },
        )

    repo = tmp_path / "mainrepo"
    repo.mkdir()
    orientation = {
        "CLAUDE.md": "# Repo orientation\n\n@ROUTING.md\n@README.md\n",
        "README.md": "# Hand-rolled readme\n\nRouting notes live here.\n",
        "ROUTING.md": "# Hand-rolled routing\n",
    }
    git("init", "-q", cwd=repo)
    for rel, content in orientation.items():
        (repo / rel).write_text(content)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "orientation", cwd=repo)
    linked = tmp_path / "linked-tree"
    git("worktree", "add", "-q", str(linked), "-b", "wt/linked", cwd=repo)

    result = run_init(tmp_path, "--here", cwd=linked)

    assert result.returncode == 0, result.stderr
    assert (linked / ".roundtable" / "agents.yaml").is_file()
    # Inherited, marker-less orientation files stay byte-identical: a linked
    # worktree must never have tracked files dirtied by onboarding.
    for rel, content in orientation.items():
        assert (linked / rel).read_text() == content
    status = git("status", "--porcelain", cwd=linked).stdout
    tracked_changes = [
        line
        for line in status.splitlines()
        if not line.endswith((".gitignore", "/")) and line.strip()
        and not line.startswith("??")
    ]
    assert tracked_changes == [], status
    # Files the repository does not carry are still created for the tree.
    assert (linked / "BRIEF.md").is_file()

    # The identical content in a standalone repository still gets the block.
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    for rel, content in orientation.items():
        (standalone / rel).write_text(content)
    control = run_init(tmp_path, "--here", cwd=standalone)
    assert control.returncode == 0, control.stderr
    assert "BEGIN Roundtable" in (standalone / "CLAUDE.md").read_text()
