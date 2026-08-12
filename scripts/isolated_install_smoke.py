#!/usr/bin/env python3
"""Exercise a release archive without inheriting the caller's install state."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping


SYSTEM_PATH_DIRECTORIES = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
HERMETIC_ENVIRONMENT_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "ROUNDTABLE_BOOTSTRAP_PYTHON",
        "TMPDIR",
    }
)
INSTALL_TIMEOUT_SECONDS = 300
VERSION_RE = re.compile(r"^[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*$")


class IsolatedInstallSmokeError(RuntimeError):
    """The release archive failed its hermetic install smoke."""


@dataclass(frozen=True)
class SmokeResult:
    version: str
    real_prefix: Path


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


def _caller_install_prefix() -> Path:
    configured = os.environ.get("ROUNDTABLE_INSTALL_PREFIX")
    selected = Path(configured).expanduser() if configured else Path.home() / ".pneu"
    return selected.resolve(strict=False)


def _path_kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _lstat_record(path: Path) -> dict[str, object]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    except OSError as error:
        raise IsolatedInstallSmokeError(
            f"cannot fingerprint real install prefix path {path}: {error}"
        ) from error
    record: dict[str, object] = {
        "kind": _path_kind(info.st_mode),
        "mtime_ns": info.st_mtime_ns,
    }
    if stat.S_ISLNK(info.st_mode):
        try:
            record["target"] = os.readlink(path)
        except OSError as error:
            raise IsolatedInstallSmokeError(
                f"cannot read real install prefix symlink {path}: {error}"
            ) from error
    return record


def fingerprint_prefix(prefix: Path) -> bytes:
    """Fingerprint the install selector and direct version inventory read-only."""

    selected = prefix.expanduser().resolve(strict=False)
    versions = selected / "versions"
    payload: dict[str, object] = {
        "prefix": str(selected),
        "prefix_state": _lstat_record(selected),
        "current": _lstat_record(selected / "current"),
        "versions": _lstat_record(versions),
        "version_entries": [],
    }
    if versions.is_dir() and not versions.is_symlink():
        try:
            entries = sorted(versions.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise IsolatedInstallSmokeError(
                f"cannot list real install versions at {versions}: {error}"
            ) from error
        payload["version_entries"] = [
            {"name": entry.name, **_lstat_record(entry)} for entry in entries
        ]
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def assert_prefix_unchanged(prefix: Path, expected: bytes) -> None:
    if fingerprint_prefix(prefix) != expected:
        raise IsolatedInstallSmokeError(
            f"real install prefix changed during isolated smoke: {prefix}"
        )


def hermetic_environment(home: Path, bootstrap_python: Path) -> dict[str, str]:
    """Construct the child environment from an allowlist, never caller state."""

    python = bootstrap_python.expanduser().resolve(strict=True)
    if not python.is_file() or not os.access(python, os.X_OK):
        raise IsolatedInstallSmokeError(
            f"bootstrap Python is not executable: {python}"
        )
    path_entries = []
    for entry in (str(python.parent), *SYSTEM_PATH_DIRECTORIES):
        if entry not in path_entries:
            path_entries.append(entry)
    environment = {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.pathsep.join(path_entries),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "ROUNDTABLE_BOOTSTRAP_PYTHON": str(python),
        "TMPDIR": str(home / "tmp"),
    }
    if frozenset(environment) != HERMETIC_ENVIRONMENT_KEYS:
        raise IsolatedInstallSmokeError(
            "internal error: hermetic environment differs from its allowlist"
        )
    return environment


def _validated_archive_members(
    archive: tarfile.TarFile,
) -> tuple[str, list[tarfile.TarInfo]]:
    members = archive.getmembers()
    if not members:
        raise IsolatedInstallSmokeError("release archive is empty")
    roots: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise IsolatedInstallSmokeError(
                f"release archive contains an unsafe path: {member.name!r}"
            )
        if not (member.isdir() or member.isfile()):
            raise IsolatedInstallSmokeError(
                f"release archive contains a non-regular member: {member.name!r}"
            )
        roots.add(path.parts[0])
    if len(roots) != 1:
        raise IsolatedInstallSmokeError(
            f"release archive must contain one top-level directory, found {sorted(roots)}"
        )
    return roots.pop(), members


def _extract_release(archive_path: Path, destination: Path) -> tuple[Path, str]:
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            root_name, members = _validated_archive_members(archive)
            archive.extractall(destination, members=members, filter="fully_trusted")
    except (OSError, tarfile.TarError) as error:
        raise IsolatedInstallSmokeError(
            f"cannot extract release archive {archive_path}: {error}"
        ) from error

    root = destination / root_name
    metadata_path = root / "BUILD-METADATA.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise IsolatedInstallSmokeError(
            f"cannot read release metadata {metadata_path}: {error}"
        ) from error
    version = metadata.get("version") if isinstance(metadata, dict) else None
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        raise IsolatedInstallSmokeError(
            f"release metadata has an invalid version: {version!r}"
        )
    expected_root = f"pneu-{version}"
    expected_archive = f"{expected_root}-macos.tar.gz"
    if root_name != expected_root or archive_path.name != expected_archive:
        raise IsolatedInstallSmokeError(
            "release archive name, root, and metadata version disagree: "
            f"{archive_path.name}, {root_name}, {version}"
        )
    install = root / "install"
    if not install.is_file() or not os.access(install, os.X_OK):
        raise IsolatedInstallSmokeError(
            f"release archive install launcher is not executable: {install}"
        )
    return root, version


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    label: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=dict(environment),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise IsolatedInstallSmokeError(
            f"{label} timed out after {INSTALL_TIMEOUT_SECONDS}s"
        ) from error
    except OSError as error:
        raise IsolatedInstallSmokeError(f"cannot run {label}: {error}") from error
    if result.returncode != 0:
        detail = _one_line(result.stderr or result.stdout or "no output")
        raise IsolatedInstallSmokeError(
            f"{label} failed with exit {result.returncode}: {detail}"
        )
    return result


def run_smoke(
    archive: Path | str,
    *,
    bootstrap_python: Path | str = Path(sys.executable),
) -> SmokeResult:
    archive_path = Path(archive).expanduser().resolve(strict=True)
    if not archive_path.is_file():
        raise IsolatedInstallSmokeError(f"release archive is not a file: {archive_path}")
    real_prefix = _caller_install_prefix()
    before = fingerprint_prefix(real_prefix)
    try:
        with tempfile.TemporaryDirectory(prefix="pneu-isolated-install-") as temporary:
            sandbox = Path(temporary)
            home = sandbox / "home"
            extract_root = sandbox / "extract"
            home.mkdir(mode=0o700)
            (home / "tmp").mkdir(mode=0o700)
            extract_root.mkdir(mode=0o700)
            payload, version = _extract_release(archive_path, extract_root)
            environment = hermetic_environment(home, Path(bootstrap_python))
            _run_checked(
                [str(payload / "install")],
                cwd=payload,
                environment=environment,
                label="release install",
            )
            prefix = home / ".pneu"
            if not prefix.is_dir():
                raise IsolatedInstallSmokeError(
                    f"release install did not create the sandbox prefix: {prefix}"
                )
            pneu = home / ".local" / "bin" / "pneu"
            if not pneu.is_file() or not os.access(pneu, os.X_OK):
                raise IsolatedInstallSmokeError(
                    f"release install did not create the sandbox pneu command: {pneu}"
                )
            report = _run_checked(
                [str(pneu), "version"],
                cwd=home,
                environment=environment,
                label="sandbox pneu version",
            )
            observed = None
            for line in report.stdout.splitlines():
                if line.startswith("version: "):
                    observed = line.removeprefix("version: ").strip()
                    break
            if observed != version:
                raise IsolatedInstallSmokeError(
                    f"sandbox pneu reported version {observed!r}, expected {version!r}"
                )
    finally:
        assert_prefix_unchanged(real_prefix, before)
    return SmokeResult(version=version, real_prefix=real_prefix)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install one pneu release archive in a hermetic temporary HOME."
    )
    parser.add_argument("archive", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_smoke(args.archive)
    except (IsolatedInstallSmokeError, OSError, ValueError) as error:
        print(f"isolated-install smoke FAIL: {_one_line(error)}", file=sys.stderr)
        return 1
    print(
        f"isolated-install smoke PASS: pneu {result.version}; "
        f"fresh HOME/.pneu used; real prefix unchanged at {result.real_prefix}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
