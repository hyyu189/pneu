"""Idempotent user-level install and uninstall for release artifacts.

The installer never downloads packages. A release install resolves every
dependency from ``--wheel-dir``. The source-tree fallback is intentionally a
developer convenience: it builds the project wheel in a temporary copy and
inherits already-installed bootstrap Python packages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import (
    LAUNCH_AGENT_LABELS,
    MANAGED_ASSETS,
    MANAGED_HELPERS,
    MANAGED_MARKER,
    MANIFEST_SCHEMA,
    TOOLS,
    VERSION,
)

SUPPORTED_PYTHON_MIN = (3, 11)
SUPPORTED_PYTHON_MAX = (3, 14)
SOURCE_BUILD_EPOCH = "946684800"


class InstallError(RuntimeError):
    """A fail-closed packaging or ownership error."""


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _prefix_default() -> Path:
    configured = os.environ.get("ROUNDTABLE_INSTALL_PREFIX")
    return _absolute(configured) if configured else Path.home() / ".roundtable"


def _link_dir_default() -> Path:
    configured = os.environ.get("ROUNDTABLE_LINK_DIR")
    return _absolute(configured) if configured else Path.home() / ".local" / "bin"


def _manifest_path(prefix: Path) -> Path:
    return prefix / "install-manifest.json"


def _harness_setup_manifest_path(prefix: Path) -> Path:
    return prefix / "harness-setup.json"


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _version_dir(prefix: Path, version: str = VERSION) -> Path:
    return prefix / "versions" / version


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_python(path: Path) -> tuple[str, tuple[int, int, int]]:
    process = subprocess.run(
        [
            str(path),
            "-c",
            (
                "import json,sys;"
                "print(json.dumps({'implementation':sys.implementation.name,"
                "'version':list(sys.version_info[:3])}))"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or f"exit {process.returncode}"
        raise InstallError(f"cannot inspect bootstrap Python {path}: {detail}")
    try:
        value = json.loads(process.stdout)
        implementation = value["implementation"]
        raw_version = value["version"]
        version = tuple(int(part) for part in raw_version)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise InstallError(f"invalid bootstrap Python probe from {path}") from error
    if (
        not isinstance(implementation, str)
        or len(version) != 3
        or any(part < 0 for part in version)
    ):
        raise InstallError(f"invalid bootstrap Python probe from {path}")
    return implementation, version


def _validate_bootstrap_python(path: Path) -> tuple[int, int, int]:
    implementation, version = _probe_python(path)
    if (
        implementation != "cpython"
        or not SUPPORTED_PYTHON_MIN <= version[:2] <= SUPPORTED_PYTHON_MAX
    ):
        raise InstallError(
            f"bootstrap Python must be CPython "
            f"{SUPPORTED_PYTHON_MIN[0]}.{SUPPORTED_PYTHON_MIN[1]} through "
            f"{SUPPORTED_PYTHON_MAX[0]}.{SUPPORTED_PYTHON_MAX[1]}; "
            f"found {implementation} {'.'.join(str(part) for part in version)} at {path}"
        )
    return version


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _atomic_symlink(path: Path, target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_manifest(prefix: Path) -> dict | None:
    path = _manifest_path(prefix)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallError(f"cannot read managed manifest {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != MANIFEST_SCHEMA:
        raise InstallError(f"refusing unknown managed manifest at {path}")
    recorded = value.get("prefix")
    if recorded != str(prefix):
        raise InstallError(
            f"managed manifest prefix mismatch: {recorded!r} != {str(prefix)!r}"
        )
    _validate_manifest_paths(prefix, value)
    return value


def _validate_manifest_paths(prefix: Path, manifest: dict) -> None:
    """Reject a corrupted manifest before any path can become a delete target."""
    raw_link_dir = manifest.get("link_dir")
    if not isinstance(raw_link_dir, str) or not Path(raw_link_dir).is_absolute():
        raise InstallError("managed manifest has an invalid link_dir")
    link_dir = Path(raw_link_dir).absolute()

    files = manifest.get("files")
    links = manifest.get("links")
    versions = manifest.get("versions")
    if not isinstance(files, dict) or not isinstance(links, dict):
        raise InstallError("managed manifest has invalid files or links")
    if not isinstance(versions, list):
        raise InstallError("managed manifest has invalid versions")

    for raw_path, digest in files.items():
        if not isinstance(raw_path, str) or not isinstance(digest, str):
            raise InstallError("managed manifest has an invalid file entry")
        path = Path(raw_path).absolute()
        if path.parent != prefix / "bin" or path.name not in TOOLS:
            raise InstallError(f"managed manifest file escapes owned paths: {path}")

    skill_link = prefix / "skills" / "shared" / "roundtable"
    for raw_path, target in links.items():
        if not isinstance(raw_path, str) or not isinstance(target, str):
            raise InstallError("managed manifest has an invalid link entry")
        path = Path(raw_path).absolute()
        if path == skill_link:
            expected = str(
                prefix
                / "current"
                / "share"
                / "roundtable"
                / "skills"
                / "shared"
                / "roundtable"
            )
            if target != expected:
                raise InstallError(
                    f"managed manifest has an invalid skill link target: {target}"
                )
            continue
        if (
            path.parent != link_dir
            or path.name not in TOOLS
            or target != str(prefix / "bin" / path.name)
        ):
            raise InstallError(f"managed manifest link escapes owned paths: {path}")

    for raw_path in versions:
        if not isinstance(raw_path, str):
            raise InstallError("managed manifest has an invalid version entry")
        path = Path(raw_path).absolute()
        if path.parent != prefix / "versions" or not path.name:
            raise InstallError(
                f"managed manifest version escapes owned paths: {path}"
            )

    version = manifest.get("version")
    expected_current = str(Path("versions") / str(version))
    if not isinstance(version, str) or manifest.get("current") != expected_current:
        raise InstallError("managed manifest has an invalid current target")

    launch_agents = manifest.get("launch_agents")
    if not isinstance(launch_agents, list) or any(
        label not in LAUNCH_AGENT_LABELS for label in launch_agents
    ):
        raise InstallError("managed manifest has invalid LaunchAgent ownership")


def _wrapper_payload(prefix: Path, tool: str) -> bytes:
    quoted = shlex.quote(str(prefix))
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f"prefix={quoted}\n"
        'export ROUNDTABLE_INSTALL_PREFIX="$prefix"\n'
        'generic_runtime=${RT_RUNTIME_DIR:-}\n'
        'legacy_runtime=${RT_CODEX_RUNTIME_DIR:-}\n'
        'if [ -n "$generic_runtime" ] && [ -n "$legacy_runtime" ] '
        '&& [ "$generic_runtime" != "$legacy_runtime" ]; then\n'
        '  echo "roundtable: RT_RUNTIME_DIR and RT_CODEX_RUNTIME_DIR '
        'must resolve to one runtime root" >&2\n'
        "  exit 2\n"
        "fi\n"
        'runtime_dir=${generic_runtime:-${legacy_runtime:-$prefix/.runtime}}\n'
        'case "$runtime_dir" in\n'
        "  /*) ;;\n"
        '  *) echo "roundtable: runtime directory must be absolute: '
        '$runtime_dir" >&2; exit 2 ;;\n'
        "esac\n"
        'export RT_RUNTIME_DIR="$runtime_dir"\n'
        'export RT_CODEX_RUNTIME_DIR="$runtime_dir"\n'
        'export PATH="$prefix/current/bin:${PATH:-/usr/bin:/bin}"\n'
        f'exec "$prefix/current/bin/{tool}" "$@"\n'
    ).encode()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _expected_links(prefix: Path, link_dir: Path) -> dict[Path, str]:
    links = {
        link_dir / tool: str(prefix / "bin" / tool)
        for tool in TOOLS
    }
    links[prefix / "skills" / "shared" / "roundtable"] = str(
        prefix
        / "current"
        / "share"
        / "roundtable"
        / "skills"
        / "shared"
        / "roundtable"
    )
    return links


def _preflight_install(
    prefix: Path,
    link_dir: Path,
    previous: dict | None,
) -> tuple[dict[Path, bytes], dict[Path, str]]:
    wrappers = {
        prefix / "bin" / tool: _wrapper_payload(prefix, tool)
        for tool in TOOLS
    }
    links = _expected_links(prefix, link_dir)
    previous_files = (previous or {}).get("files") or {}
    previous_links = (previous or {}).get("links") or {}
    conflicts = []

    for path, payload in wrappers.items():
        if not _lexists(path):
            continue
        if path.is_symlink() or not path.is_file():
            conflicts.append(f"{path}: expected a managed regular file")
            continue
        current = _sha256_path(path)
        desired = _sha256_bytes(payload)
        recorded = previous_files.get(str(path))
        if current not in {desired, recorded}:
            conflicts.append(f"{path}: modified or not managed by this installer")

    for path, target in links.items():
        if not _lexists(path):
            continue
        if path.is_symlink() and os.readlink(path) == target:
            continue
        recorded = previous_links.get(str(path))
        if path.is_symlink() and recorded and os.readlink(path) == recorded:
            continue
        conflicts.append(f"{path}: existing path is not the managed symlink")

    current = prefix / "current"
    if _lexists(current):
        recorded_current = (previous or {}).get("current")
        if not current.is_symlink():
            conflicts.append(f"{current}: expected a managed symlink")
        elif recorded_current and os.readlink(current) != recorded_current:
            conflicts.append(f"{current}: modified managed symlink")
        elif not recorded_current and not _is_relative_to(current, prefix / "versions"):
            conflicts.append(f"{current}: symlink target is outside managed versions")

    version_dir = _version_dir(prefix)
    if version_dir.exists():
        try:
            _validate_version_dir(version_dir)
        except InstallError as error:
            conflicts.append(str(error))

    if conflicts:
        rendered = "\n".join(f"  - {item}" for item in conflicts)
        raise InstallError(f"install preflight found conflicts:\n{rendered}")
    return wrappers, links


def _copy_source_for_build(source: Path, destination: Path) -> None:
    ignored_names = {
        ".git",
        ".pytest_cache",
        "__pycache__",
        "artifacts",
        "build",
        "dist",
    }

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in ignored_names
            or name.endswith(".egg-info")
            or name.endswith(".pyc")
        }

    shutil.copytree(source, destination, ignore=ignore)


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    process = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise InstallError(
            f"command failed ({process.returncode}): {shlex.join(command)}"
            + (f"\n{detail}" if detail else "")
        )


def _build_source_wheel(
    bootstrap_python: Path,
    source_root: Path,
    wheel_dir: Path,
) -> Path:
    copied = wheel_dir.parent / "source"
    _copy_source_for_build(source_root, copied)
    build_environment = os.environ.copy()
    # A source reinstall must be able to compare its input with the immutable
    # version directory. Wheel ZIP timestamps otherwise make two builds of the
    # same tree byte-different.
    build_environment["SOURCE_DATE_EPOCH"] = SOURCE_BUILD_EPOCH
    _run(
        [
            str(bootstrap_python),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            str(copied),
        ],
        env=build_environment,
    )
    matches = sorted(wheel_dir.glob(f"roundtable_messaging-{VERSION}-*.whl"))
    if len(matches) != 1:
        raise InstallError(
            f"source build produced {len(matches)} Roundtable wheels, expected one"
        )
    return matches[0]


def _release_wheel(wheel_dir: Path) -> Path:
    matches = sorted(wheel_dir.glob(f"roundtable_messaging-{VERSION}-*.whl"))
    if len(matches) != 1:
        raise InstallError(
            f"{wheel_dir} must contain exactly one "
            f"roundtable_messaging-{VERSION} wheel"
        )
    return matches[0]


def _bootstrap_has_yaml(bootstrap_python: Path) -> None:
    process = subprocess.run(
        [str(bootstrap_python), "-c", "import yaml"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise InstallError(
            "source install requires PyYAML in the bootstrap Python; "
            "use a release --wheel-dir for an isolated offline install"
        )


def _create_version(
    *,
    prefix: Path,
    bootstrap_python: Path,
    project_wheel: Path,
    wheel_dir: Path | None,
    source_mode: bool,
) -> Path:
    destination = _version_dir(prefix)
    if destination.exists():
        _validate_version_dir(
            destination,
            expected_project_wheel_sha256=_sha256_path(project_wheel),
        )
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [str(bootstrap_python), "-m", "venv"]
    if source_mode:
        command.append("--system-site-packages")
    command.append(str(destination))
    try:
        _run(command)
        installed_python = destination / "bin" / "python"
        if source_mode:
            _run(
                [
                    str(installed_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    str(project_wheel),
                ]
            )
        else:
            if wheel_dir is None:
                raise InstallError("release installation requires --wheel-dir")
            _run(
                [
                    str(installed_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--only-binary=:all:",
                    "--find-links",
                    str(wheel_dir),
                    f"roundtable-messaging=={VERSION}",
                ]
            )
        _run(
            [
                str(installed_python),
                "-c",
                "import _rtcodex, _rtlauncher, _rtlib, _rtmigrate, "
                "_rtruntime, yaml",
            ]
        )

        expected = [destination / "bin" / tool for tool in TOOLS]
        missing = [str(path) for path in expected if not path.is_file()]
        if missing:
            raise InstallError(
                "installed wheel is missing commands:\n"
                + "\n".join(f"  - {path}" for path in missing)
            )
        expected_helpers = [
            destination / "bin" / helper for helper in MANAGED_HELPERS
        ]
        missing_helpers = [
            str(path) for path in expected_helpers if not path.is_file()
        ]
        if missing_helpers:
            raise InstallError(
                "installed wheel is missing managed helpers:\n"
                + "\n".join(f"  - {path}" for path in missing_helpers)
            )

        templates = destination / "share" / "roundtable" / "templates"
        if not templates.is_dir():
            raise InstallError(f"installed wheel is missing templates: {templates}")
        missing_assets = [
            str(destination / relative)
            for relative in MANAGED_ASSETS
            if not (destination / relative).is_file()
        ]
        if missing_assets:
            raise InstallError(
                "installed wheel is missing managed onboarding assets:\n"
                + "\n".join(f"  - {path}" for path in missing_assets)
            )
        (destination / "templates").symlink_to(templates)

        marker = {
            "schema": MANIFEST_SCHEMA,
            "version": VERSION,
            "project_wheel_sha256": _sha256_path(project_wheel),
            "tools": {
                tool: _sha256_path(destination / "bin" / tool)
                for tool in TOOLS
            },
            "helpers": {
                helper: _sha256_path(destination / "bin" / helper)
                for helper in MANAGED_HELPERS
            },
            "assets": {
                relative: _sha256_path(destination / relative)
                for relative in MANAGED_ASSETS
            },
        }
        _atomic_write(
            destination / MANAGED_MARKER,
            _json_bytes(marker),
            0o600,
        )
        return destination
    except Exception:
        marker = destination / MANAGED_MARKER
        if destination.exists() and not marker.exists():
            shutil.rmtree(destination)
        raise


def _validate_version_dir(
    destination: Path,
    *,
    expected_project_wheel_sha256: str | None = None,
) -> dict:
    marker_path = destination / MANAGED_MARKER
    try:
        marker = json.loads(marker_path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallError(
            f"{destination}: cannot read matching managed marker"
        ) from error
    if (
        not isinstance(marker, dict)
        or marker.get("schema") != MANIFEST_SCHEMA
        or marker.get("version") != VERSION
    ):
        raise InstallError(f"{destination}: missing matching managed marker")
    project_digest = marker.get("project_wheel_sha256")
    if not isinstance(project_digest, str) or len(project_digest) != 64:
        raise InstallError(f"{destination}: invalid project wheel digest")
    if (
        expected_project_wheel_sha256 is not None
        and project_digest != expected_project_wheel_sha256
    ):
        raise InstallError(
            f"{destination}: installed project wheel does not match this release"
        )
    tool_digests = marker.get("tools")
    if not isinstance(tool_digests, dict) or set(tool_digests) != set(TOOLS):
        raise InstallError(f"{destination}: invalid managed tool digest set")
    for tool, expected in tool_digests.items():
        path = destination / "bin" / tool
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or not path.is_file()
            or not os.access(path, os.X_OK)
            or _sha256_path(path) != expected
        ):
            raise InstallError(f"{destination}: managed tool is missing or modified: {tool}")
    helper_digests = marker.get("helpers")
    if (
        not isinstance(helper_digests, dict)
        or set(helper_digests) != set(MANAGED_HELPERS)
    ):
        raise InstallError(f"{destination}: invalid managed helper digest set")
    for helper, expected in helper_digests.items():
        path = destination / "bin" / helper
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or not path.is_file()
            or _sha256_path(path) != expected
        ):
            raise InstallError(
                f"{destination}: managed helper is missing or modified: {helper}"
            )
    asset_digests = marker.get("assets")
    if (
        not isinstance(asset_digests, dict)
        or set(asset_digests) != set(MANAGED_ASSETS)
    ):
        raise InstallError(f"{destination}: invalid managed asset digest set")
    for relative, expected in asset_digests.items():
        path = destination / relative
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or not path.is_file()
            or _sha256_path(path) != expected
        ):
            raise InstallError(
                f"{destination}: managed onboarding asset is missing or "
                f"modified: {relative}"
            )
    templates = destination / "templates"
    expected_templates = destination / "share" / "roundtable" / "templates"
    if (
        not templates.is_symlink()
        or templates.resolve(strict=False) != expected_templates.resolve(strict=False)
        or not expected_templates.is_dir()
    ):
        raise InstallError(f"{destination}: managed templates are missing or modified")
    return marker


def _install_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roundtable-install",
        description="Install Roundtable into a versioned private virtual environment.",
    )
    parser.add_argument("--prefix", type=Path, default=_prefix_default())
    parser.add_argument("--link-dir", type=Path, default=_link_dir_default())
    parser.add_argument("--wheel-dir", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    return parser


def install_main(argv: list[str] | None = None) -> int:
    args = _install_parser().parse_args(argv)
    try:
        prefix = _absolute(args.prefix)
        link_dir = _absolute(args.link_dir)
        bootstrap_python = _absolute(args.python)
        if not bootstrap_python.is_file() or not os.access(bootstrap_python, os.X_OK):
            raise InstallError(f"bootstrap Python is not executable: {bootstrap_python}")
        _validate_bootstrap_python(bootstrap_python)

        source_root = _absolute(args.source_root) if args.source_root else None
        wheel_dir_arg = _absolute(args.wheel_dir) if args.wheel_dir else None
        if bool(source_root) == bool(wheel_dir_arg):
            raise InstallError("choose exactly one of --source-root or --wheel-dir")
        if source_root and not (source_root / "pyproject.toml").is_file():
            raise InstallError(f"not a Roundtable source root: {source_root}")
        if wheel_dir_arg and not wheel_dir_arg.is_dir():
            raise InstallError(f"wheel directory does not exist: {wheel_dir_arg}")

        prefix_existed = prefix.exists()
        previous = _load_manifest(prefix)
        wrappers, links = _preflight_install(prefix, link_dir, previous)

        with tempfile.TemporaryDirectory(prefix="roundtable-install-") as temporary:
            temporary_root = Path(temporary)
            if source_root:
                _bootstrap_has_yaml(bootstrap_python)
                build_wheels = temporary_root / "wheels"
                build_wheels.mkdir()
                project_wheel = _build_source_wheel(
                    bootstrap_python,
                    source_root,
                    build_wheels,
                )
                dependency_wheels = None
                source_mode = True
            else:
                dependency_wheels = wheel_dir_arg
                project_wheel = _release_wheel(dependency_wheels)
                source_mode = False

            version_dir = _create_version(
                prefix=prefix,
                bootstrap_python=bootstrap_python,
                project_wheel=project_wheel,
                wheel_dir=dependency_wheels,
                source_mode=source_mode,
            )

        prefix.mkdir(parents=True, exist_ok=True)
        if not prefix_existed:
            os.chmod(prefix, 0o700)
        for path, payload in wrappers.items():
            _atomic_write(path, payload, 0o755)

        current_target = str(Path("versions") / VERSION)
        _atomic_symlink(prefix / "current", current_target)
        for path, target in links.items():
            _atomic_symlink(path, target)

        old_versions = list((previous or {}).get("versions") or [])
        versions = sorted(set([*old_versions, str(version_dir)]))
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "version": VERSION,
            "prefix": str(prefix),
            "link_dir": str(link_dir),
            "current": current_target,
            "versions": versions,
            "files": {
                str(path): _sha256_bytes(payload)
                for path, payload in wrappers.items()
            },
            "links": {
                str(path): target
                for path, target in links.items()
            },
            "launch_agents": list(LAUNCH_AGENT_LABELS),
            "preserved": [
                "runtime-selected project registry and registry lock",
                str(prefix / ".runtime"),
                "registry-selected local and central mailboxes",
                "persistent UUID layout locks",
                "project .roundtable/mail bookmarks",
                "registry-adjacent migration recovery records",
                "migration archival backups and manifests",
            ],
        }
        _atomic_write(_manifest_path(prefix), _json_bytes(manifest), 0o600)
        print(f"installed Roundtable {VERSION} at {prefix}")
        print(f"commands linked in {link_dir}")
        print(f"run now: {link_dir / 'roundtable'}")
        if str(link_dir) not in os.environ.get("PATH", "").split(os.pathsep):
            print(
                "add to PATH once per shell (persist it in your shell profile): "
                f"export PATH={shlex.quote(str(link_dir))}:$PATH"
            )
        return 0
    except (InstallError, OSError) as error:
        print(f"roundtable-install: {error}", file=sys.stderr)
        return 1


def _plist_owned(path: Path, label: str, prefix: Path) -> bool:
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    if not isinstance(value, dict) or value.get("Label") != label:
        return False
    candidates = []
    for argument in value.get("ProgramArguments") or []:
        if isinstance(argument, str) and argument.startswith("/"):
            candidates.append(Path(argument))
    for key in ("StandardOutPath", "StandardErrorPath"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.startswith("/"):
            candidates.append(Path(candidate))
    return any(_is_relative_to(candidate, prefix) for candidate in candidates)


def _launch_agent_paths(prefix: Path, manifest: dict) -> list[tuple[str, Path, bool]]:
    root = Path(
        os.environ.get(
            "RT_LAUNCH_AGENTS_DIR",
            Path.home() / "Library" / "LaunchAgents",
        )
    ).expanduser()
    result = []
    for label in manifest.get("launch_agents") or []:
        path = root / f"{label}.plist"
        result.append((label, path, path.exists() and _plist_owned(path, label, prefix)))
    return result


def _uninstall_preflight(prefix: Path, manifest: dict) -> list[str]:
    conflicts = []
    for raw_path, expected_digest in (manifest.get("files") or {}).items():
        path = Path(raw_path)
        if not _lexists(path):
            continue
        if path.is_symlink() or not path.is_file():
            conflicts.append(f"{path}: managed wrapper changed type")
        elif _sha256_path(path) != expected_digest:
            conflicts.append(f"{path}: managed wrapper was modified")

    for raw_path, expected_target in (manifest.get("links") or {}).items():
        path = Path(raw_path)
        if not _lexists(path):
            continue
        if not path.is_symlink() or os.readlink(path) != expected_target:
            conflicts.append(f"{path}: managed symlink was modified")

    current = prefix / "current"
    expected_current = manifest.get("current")
    if _lexists(current) and (
        not current.is_symlink() or os.readlink(current) != expected_current
    ):
        conflicts.append(f"{current}: managed current symlink was modified")

    for raw_path in manifest.get("versions") or []:
        version_dir = Path(raw_path)
        if not version_dir.exists():
            continue
        marker = version_dir / MANAGED_MARKER
        try:
            value = json.loads(marker.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict) or value.get("schema") != MANIFEST_SCHEMA:
            conflicts.append(f"{version_dir}: managed directory marker is missing")
    return conflicts


def _launchctl() -> str:
    return os.environ.get("RT_LAUNCHCTL", "/bin/launchctl")


def _launch_domain() -> str:
    return f"gui/{os.getuid()}"


def _remove_launch_agent(label: str, path: Path) -> None:
    loaded = subprocess.run(
        [_launchctl(), "print", f"{_launch_domain()}/{label}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    if loaded:
        process = subprocess.run(
            [_launchctl(), "bootout", f"{_launch_domain()}/{label}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if process.returncode != 0:
            raise InstallError(
                f"launchctl bootout failed for {label}: {process.stderr.strip()}"
            )
    path.unlink(missing_ok=True)


def _uninstall_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roundtable-uninstall",
        description="Remove only files owned by the Roundtable install manifest.",
    )
    parser.add_argument("--prefix", type=Path, default=_prefix_default())
    parser.add_argument(
        "--purge-runtime",
        action="store_true",
        help="also remove the global ephemeral .runtime directory",
    )
    return parser


def uninstall_main(argv: list[str] | None = None) -> int:
    args = _uninstall_parser().parse_args(argv)
    try:
        prefix = _absolute(args.prefix)
        manifest = _load_manifest(prefix)
        if manifest is None:
            print(f"Roundtable is already uninstalled from {prefix}")
            return 0
        setup_manifest = _harness_setup_manifest_path(prefix)
        if _lexists(setup_manifest):
            raise InstallError(
                "harness onboarding is still installed; run "
                "`roundtable-setup remove` before removing the commands "
                f"({setup_manifest})"
            )

        conflicts = _uninstall_preflight(prefix, manifest)
        if conflicts:
            rendered = "\n".join(f"  - {item}" for item in conflicts)
            raise InstallError(
                "uninstall refused modified managed paths:\n" + rendered
            )

        for label, path, owned in _launch_agent_paths(prefix, manifest):
            if path.exists() and not owned:
                print(f"preserved non-owned LaunchAgent {path}", file=sys.stderr)
                continue
            if owned:
                _remove_launch_agent(label, path)

        for raw_path, expected_target in (manifest.get("links") or {}).items():
            path = Path(raw_path)
            if path.is_symlink() and os.readlink(path) == expected_target:
                path.unlink()

        for raw_path, expected_digest in (manifest.get("files") or {}).items():
            path = Path(raw_path)
            if path.is_file() and not path.is_symlink():
                if _sha256_path(path) == expected_digest:
                    path.unlink()

        current = prefix / "current"
        if current.is_symlink() and os.readlink(current) == manifest.get("current"):
            current.unlink()

        for raw_path in manifest.get("versions") or []:
            version_dir = Path(raw_path)
            marker = version_dir / MANAGED_MARKER
            if marker.is_file():
                value = json.loads(marker.read_text())
                if value.get("schema") == MANIFEST_SCHEMA:
                    shutil.rmtree(version_dir)

        if args.purge_runtime:
            runtime = prefix / ".runtime"
            if runtime.exists() and not runtime.is_symlink():
                shutil.rmtree(runtime)

        _manifest_path(prefix).unlink(missing_ok=True)
        for directory in (
            prefix / "skills" / "shared",
            prefix / "skills",
            prefix / "bin",
            prefix / "versions",
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        print(f"uninstalled Roundtable from {prefix}")
        print(
            "preserved project registry, UUID layout locks, "
            "registry-selected local/central mailboxes, bookmarks, "
            "migration recovery records, and archival backups"
        )
        if not args.purge_runtime:
            print(f"preserved runtime state at {prefix / '.runtime'}")
        return 0
    except (InstallError, OSError, json.JSONDecodeError) as error:
        print(f"roundtable-uninstall: {error}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="roundtable-packaging")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("install", add_help=False)
    subparsers.add_parser("uninstall", add_help=False)
    known, remaining = parser.parse_known_args()
    if known.command == "install":
        return install_main(remaining)
    return uninstall_main(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
