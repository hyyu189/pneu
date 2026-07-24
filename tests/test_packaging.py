from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from roundtable_packaging import MANAGED_ASSETS, MANAGED_HELPERS
from roundtable_packaging import cli as packaging_cli


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "install.sh"
UNINSTALL = ROOT / "scripts" / "uninstall.sh"


def packaging_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    # The suite-wide conftest fences host-local Roundtable state.  Packaging
    # tests intentionally model a brand-new HOME, so do not leak that parent
    # process fence into the installed CLI subprocesses.
    env.pop("RT_PROJECTS_FILE", None)
    env.pop("RT_RUNTIME_DIR", None)
    env.pop("RT_CODEX_RUNTIME_DIR", None)
    env.pop("CODEX_HOME", None)
    env.pop("RT_LAUNCH_AGENTS_DIR", None)
    env.pop("RT_LAUNCHCTL", None)
    env.update(
        {
            "HOME": str(home),
            "ROUNDTABLE_BOOTSTRAP_PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def run_script(
    script: Path,
    *args: str,
    home: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = packaging_env(home)
    if env:
        merged.update(env)
    return subprocess.run(
        [str(script), *args],
        cwd=ROOT,
        env=merged,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_wrapper(
    wrapper: Path,
    *,
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("RT_RUNTIME_DIR", None)
    environment.pop("RT_CODEX_RUNTIME_DIR", None)
    if overrides:
        environment.update(overrides)
    return subprocess.run(
        [str(wrapper)],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def write_runtime_probe(tmp_path: Path) -> tuple[Path, Path]:
    prefix = tmp_path / "prefix"
    target = prefix / "current" / "bin" / "probe"
    target.parent.mkdir(parents=True)
    target.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n%s\\n' \"$RT_RUNTIME_DIR\" \"$RT_CODEX_RUNTIME_DIR\"\n"
    )
    target.chmod(0o755)
    wrapper = tmp_path / "probe-wrapper"
    wrapper.write_bytes(packaging_cli._wrapper_payload(prefix, "probe"))
    wrapper.chmod(0o755)
    return prefix, wrapper


def test_wrapper_resolves_default_generic_and_legacy_runtime_roots(tmp_path):
    prefix, wrapper = write_runtime_probe(tmp_path)
    generic = (tmp_path / "generic-runtime").absolute()
    legacy = (tmp_path / "legacy-runtime").absolute()
    cases = (
        ({}, prefix / ".runtime"),
        ({"RT_RUNTIME_DIR": str(generic)}, generic),
        ({"RT_CODEX_RUNTIME_DIR": str(legacy)}, legacy),
    )

    for overrides, expected in cases:
        result = run_wrapper(wrapper, overrides=overrides)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [str(expected), str(expected)]


def test_wrapper_fails_closed_on_conflicting_runtime_roots(tmp_path):
    _, wrapper = write_runtime_probe(tmp_path)

    result = run_wrapper(
        wrapper,
        overrides={
            "RT_RUNTIME_DIR": str((tmp_path / "generic").absolute()),
            "RT_CODEX_RUNTIME_DIR": str((tmp_path / "legacy").absolute()),
        },
    )

    assert result.returncode == 2
    assert "must resolve to one runtime root" in result.stderr
    assert result.stdout == ""


def test_wrapper_rejects_relative_runtime_root(tmp_path):
    _, wrapper = write_runtime_probe(tmp_path)

    result = run_wrapper(
        wrapper,
        overrides={"RT_RUNTIME_DIR": "relative/runtime"},
    )

    assert result.returncode == 2
    assert "runtime directory must be absolute" in result.stderr


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    root = tmp_path_factory.mktemp("roundtable-wheel")
    wheel_dir = root / "wheels"
    source = root / "source"
    wheel_dir.mkdir()
    shutil.copytree(
        ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            "__pycache__",
            "*.egg-info",
            "*.pyc",
            "build",
            "dist",
        ),
    )
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            str(source),
        ],
        cwd=source,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    matches = list(wheel_dir.glob("roundtable_messaging-0.1.8-*.whl"))
    assert len(matches) == 1
    return matches[0]


def test_wheel_contains_commands_helpers_templates_and_uninstaller(built_wheel):
    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points = archive.read(entry_points_name).decode("utf-8")
        skill_name = next(
            name
            for name in names
            if name.endswith(
                ".data/data/share/roundtable/skills/shared/roundtable/SKILL.md"
            )
        )
        skill = archive.read(skill_name).decode("utf-8")

    assert "roundtable_packaging/cli.py" in names
    assert "roundtable_packaging/setup.py" in names
    assert "roundtable_packaging/migrate.py" not in names
    assert "_rtruntime.py" in names
    assert any(name.endswith(".data/scripts/roundtable") for name in names)
    assert any(name.endswith(".data/scripts/rt-say") for name in names)
    assert any(
        name.endswith(".data/scripts/rt-codex-session-start") for name in names
    )
    assert any(name.endswith(".data/scripts/_rtlib.py") for name in names)
    assert any(name.endswith(".data/scripts/_rtruntime.py") for name in names)
    assert any(
        name.endswith(".data/data/share/roundtable/templates/agents.yaml.tmpl")
        for name in names
    )
    assert any(
        name.endswith(
            ".data/data/share/roundtable/integrations/hermes/"
            "roundtable/plugin.yaml"
        )
        for name in names
    )
    assert any(
        name.endswith(
            ".data/data/share/roundtable/integrations/hermes/"
            "roundtable/__init__.py"
        )
        for name in names
    )
    assert "roundtable-migrate" not in entry_points
    assert "trusted SessionStart hook" in skill
    assert "diagnostic fallback only" in skill
    assert "rt-codex-daemon install --reload" not in skill
    assert "then self-register in the first" not in skill


def test_clean_home_install_is_idempotent_and_uninstall_preserves_state(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    prefix = home / ".roundtable"
    link_dir = home / ".local" / "bin"

    first = run_script(
        INSTALL,
        "--prefix",
        str(prefix),
        "--link-dir",
        str(link_dir),
        home=home,
    )
    assert first.returncode == 0, first.stderr
    assert f"run now: {link_dir / 'roundtable'}" in first.stdout

    manifest_path = prefix / "install-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema"] == "roundtable.install.v1"
    assert (prefix / "current").is_symlink()
    marker = json.loads(
        (prefix / "current" / ".roundtable-managed.json").read_text()
    )
    assert set(marker["helpers"]) == set(MANAGED_HELPERS)
    assert set(marker["assets"]) == set(MANAGED_ASSETS)
    assert (link_dir / "rt-say").is_symlink()
    assert (prefix / "bin" / "rt-say").stat().st_mode & stat.S_IXUSR
    wrapper = (prefix / "bin" / "rt-say").read_text()
    assert 'export RT_RUNTIME_DIR="$runtime_dir"' in wrapper
    assert 'export RT_CODEX_RUNTIME_DIR="$runtime_dir"' in wrapper

    root_probe = subprocess.run(
        [
            str(prefix / "current" / "bin" / "python"),
            "-c",
            (
                "import _rtcodex, _rtlauncher, _rtlib, _rtruntime; "
                "print(_rtcodex.ROUND_ROOT)"
            ),
        ],
        env={
            **packaging_env(home),
            "ROUNDTABLE_INSTALL_PREFIX": str(prefix),
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert root_probe.returncode == 0, root_probe.stderr
    assert root_probe.stdout.strip() == str(prefix / "current")

    smoke = subprocess.run(
        [str(link_dir / "roundtable-smoke")],
        env=packaging_env(home),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert smoke.returncode == 0, smoke.stderr
    assert json.loads(smoke.stdout)["status"] == "passed"

    wrapper_hashes = {
        path: digest(Path(path))
        for path in manifest["files"]
    }
    second = run_script(
        INSTALL,
        "--prefix",
        str(prefix),
        "--link-dir",
        str(link_dir),
        home=home,
    )
    assert second.returncode == 0, second.stderr
    assert wrapper_hashes == {
        path: digest(Path(path))
        for path in manifest["files"]
    }

    project_parent = tmp_path / "projects"
    project_parent.mkdir()
    initialized = subprocess.run(
        [
            str(link_dir / "roundtable-init"),
            "--no-git",
            "--parent",
            str(project_parent),
            "demo",
        ],
        cwd=tmp_path,
        env=packaging_env(home),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert initialized.returncode == 0, initialized.stderr

    project = project_parent / "demo"
    inbox = project / ".roundtable" / "inbox" / "claude" / "new"
    inbox.mkdir(parents=True)
    mail = inbox / "keep.md"
    mail.write_text("[codex→claude fyi id=keep] preserve me\n")
    runtime = prefix / ".runtime"
    runtime.mkdir()
    runtime_file = runtime / "keep.json"
    runtime_file.write_text("{}\n")
    registry = prefix / "projects.yaml"
    registry_before = registry.read_bytes()

    removed = run_script(
        UNINSTALL,
        "--prefix",
        str(prefix),
        home=home,
    )
    assert removed.returncode == 0, removed.stderr
    assert registry.read_bytes() == registry_before
    assert runtime_file.read_text() == "{}\n"
    assert mail.read_text().endswith("preserve me\n")
    assert not (link_dir / "rt-say").exists()
    assert not manifest_path.exists()

    again = run_script(
        UNINSTALL,
        "--prefix",
        str(prefix),
        home=home,
    )
    assert again.returncode == 0, again.stderr
    assert "already uninstalled" in again.stdout


def test_install_conflict_fails_before_creating_version(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    prefix = home / ".roundtable"
    link_dir = home / ".local" / "bin"
    link_dir.mkdir(parents=True)
    conflict = link_dir / "rt-say"
    conflict.write_text("owned by user\n")

    process = run_script(
        INSTALL,
        "--prefix",
        str(prefix),
        "--link-dir",
        str(link_dir),
        home=home,
    )

    assert process.returncode == 1
    assert "install preflight found conflicts" in process.stderr
    assert conflict.read_text() == "owned by user\n"
    assert not (prefix / "versions").exists()
    assert not (prefix / "install-manifest.json").exists()


def test_modified_wrapper_makes_uninstall_fail_closed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    prefix = home / ".roundtable"
    link_dir = home / ".local" / "bin"
    installed = run_script(
        INSTALL,
        "--prefix",
        str(prefix),
        "--link-dir",
        str(link_dir),
        home=home,
    )
    assert installed.returncode == 0, installed.stderr

    wrapper = prefix / "bin" / "rt-say"
    wrapper.write_text("#!/bin/sh\nexit 99\n")
    removed = run_script(
        UNINSTALL,
        "--prefix",
        str(prefix),
        home=home,
    )

    assert removed.returncode == 1
    assert "managed wrapper was modified" in removed.stderr
    assert wrapper.exists()
    assert (prefix / "current").is_symlink()
    assert (prefix / "install-manifest.json").exists()


@pytest.mark.parametrize("setup_marker", ["file", "dangling-symlink"])
def test_uninstall_refuses_to_leave_managed_harness_config_broken(
    tmp_path,
    setup_marker,
):
    home = tmp_path / "home"
    home.mkdir()
    prefix = home / ".roundtable"
    link_dir = home / ".local" / "bin"
    installed = run_script(
        INSTALL,
        "--prefix",
        str(prefix),
        "--link-dir",
        str(link_dir),
        home=home,
    )
    assert installed.returncode == 0, installed.stderr
    setup_manifest = prefix / "harness-setup.json"
    if setup_marker == "file":
        setup_manifest.write_text("{}\n")
    else:
        setup_manifest.symlink_to(prefix / "missing-setup-manifest.json")

    refused = run_script(
        UNINSTALL,
        "--prefix",
        str(prefix),
        home=home,
    )

    assert refused.returncode == 1
    assert "roundtable-setup remove" in refused.stderr
    assert (prefix / "install-manifest.json").is_file()
    assert (link_dir / "rt-say").is_symlink()

    setup_manifest.unlink()
    removed = run_script(
        UNINSTALL,
        "--prefix",
        str(prefix),
        home=home,
    )
    assert removed.returncode == 0, removed.stderr


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("rt-say", "managed tool is missing or modified: rt-say"),
        ("_rtruntime.py", "managed helper is missing or modified: _rtruntime.py"),
        (
            "../share/roundtable/integrations/hermes/roundtable/plugin.yaml",
            "managed onboarding asset is missing or modified",
        ),
    ],
)
def test_same_version_reinstall_rejects_modified_installed_runtime(
    tmp_path,
    relative,
    expected,
):
    home = tmp_path / "home"
    home.mkdir()
    prefix = home / ".roundtable"
    link_dir = home / ".local" / "bin"
    installed = run_script(
        INSTALL,
        "--prefix",
        str(prefix),
        "--link-dir",
        str(link_dir),
        home=home,
    )
    assert installed.returncode == 0, installed.stderr

    managed = prefix / "current" / "bin" / relative
    managed.write_text("#!/bin/sh\nexit 99\n")
    repeated = run_script(
        INSTALL,
        "--prefix",
        str(prefix),
        "--link-dir",
        str(link_dir),
        home=home,
    )

    assert repeated.returncode == 1
    assert expected in repeated.stderr
    assert managed.read_text() == "#!/bin/sh\nexit 99\n"


def test_install_shell_rejects_unsupported_bootstrap_python(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    process = run_script(
        INSTALL,
        "--prefix",
        str(home / ".roundtable"),
        "--link-dir",
        str(home / ".local" / "bin"),
        home=home,
        env={"ROUNDTABLE_BOOTSTRAP_PYTHON": "/usr/bin/false"},
    )

    assert process.returncode == 1
    assert "must be CPython 3.11 through 3.14" in process.stderr
    assert not (home / ".roundtable").exists()


def test_install_and_uninstall_find_versioned_python_after_unsupported_python3(
    tmp_path,
):
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to("/usr/bin/false")
    (fake_bin / "python3.14").symlink_to(sys.executable)
    environment = packaging_env(home)
    environment.pop("ROUNDTABLE_BOOTSTRAP_PYTHON")
    # An activated environment now takes priority over the version-name scan,
    # so clear it here to keep exercising the versioned-command fallback.
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("CONDA_PREFIX", None)
    environment["PATH"] = os.pathsep.join(
        (str(fake_bin), "/usr/bin", "/bin")
    )

    process = subprocess.run(
        [
            str(INSTALL),
            "--prefix",
            str(home / ".roundtable"),
            "--link-dir",
            str(home / ".local" / "bin"),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert process.returncode == 0, process.stderr
    assert (home / ".roundtable" / "current").is_symlink()

    removed = subprocess.run(
        [
            str(UNINSTALL),
            "--prefix",
            str(home / ".roundtable"),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert removed.returncode == 0, removed.stderr
    assert not (home / ".roundtable" / "current").exists()


def test_tampered_manifest_cannot_delete_outside_prefix(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    prefix = home / ".roundtable"
    link_dir = home / ".local" / "bin"
    installed = run_script(
        INSTALL,
        "--prefix",
        str(prefix),
        "--link-dir",
        str(link_dir),
        home=home,
    )
    assert installed.returncode == 0, installed.stderr

    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("owned by user\n")
    manifest_path = prefix / "install-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["versions"] = [str(outside)]
    manifest_path.write_text(json.dumps(manifest))

    removed = run_script(
        UNINSTALL,
        "--prefix",
        str(prefix),
        home=home,
    )

    assert removed.returncode == 1
    assert "version escapes owned paths" in removed.stderr
    assert sentinel.read_text() == "owned by user\n"
    assert (prefix / "current").is_symlink()


def test_owned_launch_agents_are_booted_out_but_foreign_plist_is_preserved(
    tmp_path,
):
    home = tmp_path / "home"
    home.mkdir()
    prefix = home / ".roundtable"
    link_dir = home / ".local" / "bin"
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    trace = tmp_path / "launchctl.jsonl"
    fake_launchctl = tmp_path / "launchctl"
    fake_launchctl.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {str(trace)!r}\n"
        "exit 0\n"
    )
    fake_launchctl.chmod(0o755)

    installed = run_script(
        INSTALL,
        "--prefix",
        str(prefix),
        "--link-dir",
        str(link_dir),
        home=home,
    )
    assert installed.returncode == 0, installed.stderr

    owned = launch_agents / "com.roundtable.codex-wake.plist"
    owned.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.roundtable.codex-wake",
                "ProgramArguments": [str(prefix / "bin" / "rt-codex-wake"), "run"],
            }
        )
    )
    foreign = launch_agents / "com.roundtable.codex-app-server.plist"
    foreign_payload = plistlib.dumps(
        {
            "Label": "com.roundtable.codex-app-server",
            "ProgramArguments": ["/usr/local/bin/codex", "app-server"],
            "StandardErrorPath": str(tmp_path / "foreign.log"),
        }
    )
    foreign.write_bytes(foreign_payload)

    removed = run_script(
        UNINSTALL,
        "--prefix",
        str(prefix),
        home=home,
        env={
            "RT_LAUNCH_AGENTS_DIR": str(launch_agents),
            "RT_LAUNCHCTL": str(fake_launchctl),
        },
    )

    assert removed.returncode == 0, removed.stderr
    assert not owned.exists()
    assert foreign.read_bytes() == foreign_payload
    commands = trace.read_text().splitlines()
    assert any(line.startswith("print ") for line in commands)
    assert any("bootout" in line and "com.roundtable.codex-wake" in line for line in commands)
    assert "preserved non-owned LaunchAgent" in removed.stderr


def write_identifying_python(
    path: Path,
    *,
    name: str,
    trace: Path,
    supported: bool = True,
    can_build: bool = True,
) -> Path:
    """Write a fake interpreter that answers the discovery probes.

    It reports CPython support and setuptools build capability through the
    exact `-c` probes the installer scripts run, and records its own name into
    ``trace`` when finally exec'd as the bootstrap (``-m ...``), so a test can
    assert which candidate discovery selected without a real install.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then\n'
        '  case "$2" in\n'
        f"    *sys.implementation*) exit {0 if supported else 1} ;;\n"
        f"    *setuptools.build_meta*) exit {0 if can_build else 1} ;;\n"
        "    *) exit 0 ;;\n"
        "  esac\n"
        "fi\n"
        'if [ "$1" = "-m" ]; then\n'
        f"  printf '%s\\n' {shlex.quote(name)} > {shlex.quote(str(trace))}\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    path.chmod(0o755)
    return path


def discovery_env(home: Path, **overrides: str) -> dict[str, str]:
    env = packaging_env(home)
    env.pop("ROUNDTABLE_BOOTSTRAP_PYTHON", None)
    env.pop("VIRTUAL_ENV", None)
    env.pop("CONDA_PREFIX", None)
    env.update(overrides)
    return env


def run_installer(script: Path, *args: str, env: dict[str, str]):
    return subprocess.run(
        [str(script), *args],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


@pytest.mark.parametrize("activation_var", ["CONDA_PREFIX", "VIRTUAL_ENV"])
def test_install_prefers_activated_env_over_higher_version_name(
    tmp_path,
    activation_var,
):
    home = tmp_path / "home"
    home.mkdir()
    trace = tmp_path / "chosen.txt"
    path_bin = tmp_path / "path-bin"
    write_identifying_python(
        path_bin / "python3.14",
        name="path-python3.14",
        trace=trace,
    )
    activated = tmp_path / "activated"
    write_identifying_python(
        activated / "bin" / "python",
        name="activated-python",
        trace=trace,
    )

    env = discovery_env(
        home,
        PATH=os.pathsep.join((str(path_bin), "/usr/bin", "/bin")),
        **{activation_var: str(activated)},
    )
    process = run_installer(
        INSTALL,
        "--prefix",
        str(home / ".roundtable"),
        "--link-dir",
        str(home / ".local" / "bin"),
        env=env,
    )

    assert process.returncode == 0, process.stderr
    assert trace.read_text().strip() == "activated-python"


def test_uninstall_prefers_activated_env_over_higher_version_name(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    trace = tmp_path / "chosen.txt"
    path_bin = tmp_path / "path-bin"
    write_identifying_python(
        path_bin / "python3.14",
        name="path-python3.14",
        trace=trace,
    )
    activated = tmp_path / "activated"
    write_identifying_python(
        activated / "bin" / "python",
        name="activated-python",
        trace=trace,
    )

    env = discovery_env(
        home,
        PATH=os.pathsep.join((str(path_bin), "/usr/bin", "/bin")),
        CONDA_PREFIX=str(activated),
    )
    process = run_installer(
        UNINSTALL,
        "--prefix",
        str(home / ".roundtable"),
        env=env,
    )

    assert process.returncode == 0, process.stderr
    assert trace.read_text().strip() == "activated-python"


def test_install_source_mode_skips_candidate_without_setuptools(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    trace = tmp_path / "chosen.txt"
    path_bin = tmp_path / "path-bin"
    write_identifying_python(
        path_bin / "python3.14",
        name="python3.14",
        trace=trace,
        can_build=False,
    )
    write_identifying_python(
        path_bin / "python3.13",
        name="python3.13",
        trace=trace,
        can_build=True,
    )

    env = discovery_env(
        home,
        PATH=os.pathsep.join((str(path_bin), "/usr/bin", "/bin")),
    )
    process = run_installer(
        INSTALL,
        "--prefix",
        str(home / ".roundtable"),
        "--link-dir",
        str(home / ".local" / "bin"),
        env=env,
    )

    assert process.returncode == 0, process.stderr
    assert trace.read_text().strip() == "python3.13"
    assert "skipping" in process.stderr
    assert "python3.14" in process.stderr
    assert "setuptools unavailable" in process.stderr


def test_install_explicit_python_fails_closed_when_cannot_build(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    trace = tmp_path / "chosen.txt"
    explicit = write_identifying_python(
        tmp_path / "explicit-python",
        name="explicit-python",
        trace=trace,
        can_build=False,
    )

    env = discovery_env(home, ROUNDTABLE_BOOTSTRAP_PYTHON=str(explicit))
    process = run_installer(
        INSTALL,
        "--prefix",
        str(home / ".roundtable"),
        "--link-dir",
        str(home / ".local" / "bin"),
        env=env,
    )

    assert process.returncode == 1
    assert "cannot build from source" in process.stderr
    assert "setuptools unavailable" in process.stderr
    assert not trace.exists()
    assert not (home / ".roundtable").exists()


def test_install_wheel_mode_accepts_python_without_setuptools(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    trace = tmp_path / "chosen.txt"
    wheel_python = write_identifying_python(
        tmp_path / "wheel-python",
        name="wheel-python",
        trace=trace,
        can_build=False,
    )
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()

    env = discovery_env(home, ROUNDTABLE_BOOTSTRAP_PYTHON=str(wheel_python))
    process = run_installer(
        INSTALL,
        "--wheel-dir",
        str(wheel_dir),
        "--prefix",
        str(home / ".roundtable"),
        "--link-dir",
        str(home / ".local" / "bin"),
        env=env,
    )

    assert process.returncode == 0, process.stderr
    assert trace.read_text().strip() == "wheel-python"
    assert "cannot build from source" not in process.stderr
    assert "skipping" not in process.stderr
