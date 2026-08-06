#!/usr/bin/env python3
"""Build a deterministic, offline-installable macOS release artifact.

Release inputs come exclusively from ``git archive HEAD`` and a complete,
hash-verified PyYAML wheel matrix. The command refuses a dirty repository and
never asks pip to resolve or download build dependencies.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


RELEASE_SCHEMA = "pneu.release.v1"
PROJECT_NAME = "pneu"
DEPENDENCY_NAME = "PyYAML"
DEPENDENCY_VERSION = "6.0.3"


@dataclass(frozen=True)
class DependencyWheel:
    filename: str
    url: str
    sha256: str
    python: str
    architecture: str


# Source: https://pypi.org/pypi/PyYAML/6.0.3/json
# Locked from the official PyPI release metadata on 2026-07-18.
PYYAML_WHEELS = (
    DependencyWheel(
        filename="pyyaml-6.0.3-cp311-cp311-macosx_10_13_x86_64.whl",
        url=(
            "https://files.pythonhosted.org/packages/6d/16/"
            "a95b6757765b7b031c9374925bb718d55e0a9ba8a1b6a12d25962ea44347/"
            "pyyaml-6.0.3-cp311-cp311-macosx_10_13_x86_64.whl"
        ),
        sha256="44edc647873928551a01e7a563d7452ccdebee747728c1080d881d68af7b997e",
        python="cp311",
        architecture="x86_64",
    ),
    DependencyWheel(
        filename="pyyaml-6.0.3-cp311-cp311-macosx_11_0_arm64.whl",
        url=(
            "https://files.pythonhosted.org/packages/16/19/"
            "13de8e4377ed53079ee996e1ab0a9c33ec2faf808a4647b7b4c0d46dd239/"
            "pyyaml-6.0.3-cp311-cp311-macosx_11_0_arm64.whl"
        ),
        sha256="652cb6edd41e718550aad172851962662ff2681490a8a711af6a4d288dd96824",
        python="cp311",
        architecture="arm64",
    ),
    DependencyWheel(
        filename="pyyaml-6.0.3-cp312-cp312-macosx_10_13_x86_64.whl",
        url=(
            "https://files.pythonhosted.org/packages/d1/33/"
            "422b98d2195232ca1826284a76852ad5a86fe23e31b009c9886b2d0fb8b2/"
            "pyyaml-6.0.3-cp312-cp312-macosx_10_13_x86_64.whl"
        ),
        sha256="7f047e29dcae44602496db43be01ad42fc6f1cc0d8cd6c83d342306c32270196",
        python="cp312",
        architecture="x86_64",
    ),
    DependencyWheel(
        filename="pyyaml-6.0.3-cp312-cp312-macosx_11_0_arm64.whl",
        url=(
            "https://files.pythonhosted.org/packages/89/a0/"
            "6cf41a19a1f2f3feab0e9c0b74134aa2ce6849093d5517a0c550fe37a648/"
            "pyyaml-6.0.3-cp312-cp312-macosx_11_0_arm64.whl"
        ),
        sha256="fc09d0aa354569bc501d4e787133afc08552722d3ab34836a80547331bb5d4a0",
        python="cp312",
        architecture="arm64",
    ),
    DependencyWheel(
        filename="pyyaml-6.0.3-cp313-cp313-macosx_10_13_x86_64.whl",
        url=(
            "https://files.pythonhosted.org/packages/d1/11/"
            "0fd08f8192109f7169db964b5707a2f1e8b745d4e239b784a5a1dd80d1db/"
            "pyyaml-6.0.3-cp313-cp313-macosx_10_13_x86_64.whl"
        ),
        sha256="8da9669d359f02c0b91ccc01cac4a67f16afec0dac22c2ad09f46bee0697eba8",
        python="cp313",
        architecture="x86_64",
    ),
    DependencyWheel(
        filename="pyyaml-6.0.3-cp313-cp313-macosx_11_0_arm64.whl",
        url=(
            "https://files.pythonhosted.org/packages/b1/16/"
            "95309993f1d3748cd644e02e38b75d50cbc0d9561d21f390a76242ce073f/"
            "pyyaml-6.0.3-cp313-cp313-macosx_11_0_arm64.whl"
        ),
        sha256="2283a07e2c21a2aa78d9c4442724ec1eb15f5e42a723b99cb3d822d48f5f7ad1",
        python="cp313",
        architecture="arm64",
    ),
    DependencyWheel(
        filename="pyyaml-6.0.3-cp314-cp314-macosx_10_13_x86_64.whl",
        url=(
            "https://files.pythonhosted.org/packages/9d/8c/"
            "f4bd7f6465179953d3ac9bc44ac1a8a3e6122cf8ada906b4f96c60172d43/"
            "pyyaml-6.0.3-cp314-cp314-macosx_10_13_x86_64.whl"
        ),
        sha256="8d1fab6bb153a416f9aeb4b8763bc0f22a5586065f86f7664fc23339fc1c1fac",
        python="cp314",
        architecture="x86_64",
    ),
    DependencyWheel(
        filename="pyyaml-6.0.3-cp314-cp314-macosx_11_0_arm64.whl",
        url=(
            "https://files.pythonhosted.org/packages/bd/9c/"
            "4d95bb87eb2063d20db7b60faa3840c1b18025517ae857371c4dd55a6b3a/"
            "pyyaml-6.0.3-cp314-cp314-macosx_11_0_arm64.whl"
        ),
        sha256="34d5fcd24b8445fadc33f9cf348c1047101756fd760b4dacb5c3e99755703310",
        python="cp314",
        architecture="arm64",
    ),
)

EXPECTED_MATRIX = {
    (python, architecture)
    for python in ("cp311", "cp312", "cp313", "cp314")
    for architecture in ("arm64", "x86_64")
}

OUTER_STATIC_FILES = {
    "BUILD-METADATA.json",
    "CREDITS.md",
    "LICENSE",
    "NOTICE",
    "PROVENANCE.md",
    "README.md",
    "SHA256SUMS",
    "docs/architecture.md",
    "docs/compatibility.md",
    "docs/install.md",
    "docs/provenance/source-commits.tsv",
    "docs/release.md",
    "install",
    "pneu_packaging/__init__.py",
    "pneu_packaging/cli.py",
    "pneu_packaging/setup.py",
    "scripts/install.sh",
    "scripts/uninstall.sh",
    "uninstall",
}

FORBIDDEN_COMPONENTS = {
    ".git",
    ".pytest_cache",
    ".roundtable",
    ".runtime",
    "__pycache__",
    "inbox",
    "layout-locks",
    "locks",
    "mail",
    "messages",
}

REQUIRED_PROJECT_ROOT_FILES = frozenset(
    {
        "_rtcodex.py",
        "_rtlauncher.py",
        "_rtlib.py",
        "_rtmigrate.py",
        "_rtruntime.py",
    }
)
REQUIRED_PROJECT_PACKAGE_FILES = frozenset(
    {
        "pneu_packaging/__init__.py",
        "pneu_packaging/cli.py",
        "pneu_packaging/setup.py",
        "pneu_packaging/smoke.py",
    }
)
REQUIRED_PROJECT_DATA_FILES = frozenset(
    {
        "share/pneu/integrations/hermes/pneu/__init__.py",
        "share/pneu/integrations/hermes/pneu/plugin.yaml",
        "share/pneu/integrations/openclaw/roundtable/__init__.py",
        "share/pneu/integrations/grok/roundtable/__init__.py",
        "share/pneu/skills/shared/pneu/SKILL.md",
    }
)
REQUIRED_PROJECT_SCRIPTS = frozenset(
    {
        "_rtcodex.py",
        "_rtlauncher.py",
        "_rtlib.py",
        "_rtmigrate.py",
        "_rtruntime.py",
        "pneu",
        "roundtable",
        "roundtable-init",
        "rt-ack",
        "rt-claude",
        "rt-codex",
        "rt-codex-daemon",
        "rt-codex-session-start",
        "rt-codex-wake",
        "rt-doctor",
        "rt-hermes",
        "rt-inbox",
        "rt-openclaw",
        "rt-openclaw-wake",
        "rt-grok",
        "rt-grok-wake",
        "rt-projects",
        "rt-refresh",
        "rt-resolve",
        "rt-say",
        "rt-startup-advisory",
        "rt-stop-gate",
        "rt-wait-inbox",
        "rt-worktree",
    }
)


class ReleaseError(RuntimeError):
    """A release precondition or integrity failure."""


@dataclass(frozen=True)
class BuildResult:
    artifact: Path
    outer_checksums: Path
    version: str
    commit: str
    sha256: str


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _atomic_write(path: Path, payload: bytes, mode: int = 0o644) -> None:
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


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    binary: bool = False,
) -> str | bytes:
    process = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
        check=False,
    )
    if process.returncode != 0:
        stderr = process.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        detail = (stderr or "").strip()
        raise ReleaseError(
            f"command failed ({process.returncode}): {' '.join(command)}"
            + (f"\n{detail}" if detail else "")
        )
    return process.stdout


def _git(repo: Path, *arguments: str, binary: bool = False) -> str | bytes:
    return _run(["git", *arguments], cwd=repo, binary=binary)


def _assert_clean_repo(repo: Path) -> tuple[str, int]:
    try:
        root = Path(str(_git(repo, "rev-parse", "--show-toplevel")).strip()).resolve()
    except ReleaseError as error:
        raise ReleaseError(f"not a Git repository: {repo}") from error
    if root != repo.resolve():
        raise ReleaseError(f"--repo must be the Git project root: {root}")

    status = str(
        _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    ).strip()
    if status:
        sample = "\n".join(status.splitlines()[:20])
        raise ReleaseError(
            "refusing release from a dirty worktree; commit or remove every change:\n"
            + sample
        )
    commit = str(_git(repo, "rev-parse", "--verify", "HEAD^{commit}")).strip()
    timestamp_text = str(_git(repo, "show", "-s", "--format=%ct", commit)).strip()
    try:
        timestamp = int(timestamp_text)
    except ValueError as error:
        raise ReleaseError(f"invalid commit timestamp: {timestamp_text!r}") from error
    return commit, timestamp


def _safe_extract_git_archive(payload: bytes, destination: Path) -> None:
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = archive.getmembers()
        for member in members:
            pure = PurePosixPath(member.name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or any(part in FORBIDDEN_COMPONENTS for part in pure.parts)
            ):
                raise ReleaseError(f"unsafe path in git archive: {member.name}")
        for member in members:
            target = destination / PurePosixPath(member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, member.mode & 0o777)
                continue
            if member.issym():
                link = PurePosixPath(member.linkname)
                if (
                    link.is_absolute()
                    or ".." in link.parts
                    or any(part in FORBIDDEN_COMPONENTS for part in link.parts)
                ):
                    raise ReleaseError(
                        f"unsafe symlink target in git archive: {member.name} -> "
                        f"{member.linkname}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
                continue
            if not member.isfile():
                raise ReleaseError(
                    f"unsupported entry type in git archive: {member.name}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseError(f"cannot read git archive entry: {member.name}")
            with source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            os.chmod(target, member.mode & 0o777)


def _source_from_commit(repo: Path, commit: str, destination: Path) -> None:
    payload = _git(
        repo,
        "archive",
        "--format=tar",
        commit,
        binary=True,
    )
    if not isinstance(payload, bytes):
        raise ReleaseError("git archive returned non-binary output")
    _safe_extract_git_archive(payload, destination)


def _project_version(source: Path) -> str:
    try:
        value = tomllib.loads((source / "pyproject.toml").read_text())
        version = value["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise ReleaseError(f"cannot read project version: {error}") from error
    if not isinstance(version, str) or not version:
        raise ReleaseError("project version must be a non-empty string")
    try:
        tree = ast.parse((source / "pneu_packaging" / "__init__.py").read_text())
    except (OSError, SyntaxError) as error:
        raise ReleaseError(f"cannot read packaging version: {error}") from error
    packaging_version = None
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name) and target.id == "VERSION"
            for target in targets
        ):
            continue
        value_node = node.value
        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            packaging_version = value_node.value
            break
    if packaging_version != version:
        raise ReleaseError(
            f"version mismatch: pyproject={version!r} "
            f"pneu_packaging.VERSION={packaging_version!r}"
        )
    return version


def _build_toolchain(python: Path) -> dict:
    output = _run(
        [
            str(python),
            "-c",
            (
                "import importlib.metadata,json,platform,sys;"
                "print(json.dumps({"
                "'implementation':sys.implementation.name,"
                "'python':platform.python_version(),"
                "'pip':importlib.metadata.version('pip'),"
                "'setuptools':importlib.metadata.version('setuptools'),"
                "'wheel':importlib.metadata.version('wheel')"
                "},sort_keys=True))"
            ),
        ]
    )
    try:
        value = json.loads(str(output))
    except json.JSONDecodeError as error:
        raise ReleaseError("build toolchain probe returned invalid JSON") from error
    required = {"implementation", "python", "pip", "setuptools", "wheel"}
    if not isinstance(value, dict) or set(value) != required or not all(
        isinstance(item, str) and item for item in value.values()
    ):
        raise ReleaseError("build toolchain probe returned invalid metadata")
    return value


def _build_project_wheel(
    source: Path,
    wheel_dir: Path,
    *,
    python: Path,
    source_date_epoch: int,
    version: str,
) -> Path:
    wheel_dir.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(source_date_epoch),
        }
    )
    _run(
        [
            str(python),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            str(source),
        ],
        env=environment,
    )
    matches = sorted(wheel_dir.glob(f"pneu-{version}-*.whl"))
    if len(matches) != 1:
        raise ReleaseError(
            f"expected one project wheel for {version}, found {len(matches)}"
        )
    wheel = matches[0]
    if not wheel.name.endswith("-py3-none-any.whl"):
        raise ReleaseError(f"project wheel is not pure Python: {wheel.name}")
    _validate_project_wheel(wheel, version)
    return wheel


def _validate_project_wheel(wheel: Path, version: str) -> None:
    dist_info = f"pneu-{version}.dist-info/"
    data_prefix = f"pneu-{version}.data/"
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
    if not names:
        raise ReleaseError(f"project wheel is empty: {wheel}")
    required = {
        *REQUIRED_PROJECT_ROOT_FILES,
        *REQUIRED_PROJECT_PACKAGE_FILES,
        *(
            f"{data_prefix}data/{path}"
            for path in REQUIRED_PROJECT_DATA_FILES
        ),
        *(
            f"{data_prefix}scripts/{script}"
            for script in REQUIRED_PROJECT_SCRIPTS
        ),
    }
    missing = required - set(names)
    if missing:
        raise ReleaseError(
            "project wheel is missing required paths: "
            + ", ".join(sorted(missing))
        )
    for name in names:
        pure = PurePosixPath(name)
        if any(part in FORBIDDEN_COMPONENTS for part in pure.parts):
            raise ReleaseError(f"forbidden runtime path in project wheel: {name}")
        allowed = (
            name in REQUIRED_PROJECT_ROOT_FILES
            or name.startswith("pneu_packaging/")
            or name.startswith(dist_info)
            or name.startswith(f"{data_prefix}scripts/")
            or name.startswith(f"{data_prefix}data/share/pneu/")
        )
        if not allowed:
            raise ReleaseError(f"unexpected path in project wheel: {name}")


def _validate_matrix(specs: tuple[DependencyWheel, ...]) -> None:
    matrix = {(spec.python, spec.architecture) for spec in specs}
    if matrix != EXPECTED_MATRIX:
        missing = sorted(EXPECTED_MATRIX - matrix)
        unexpected = sorted(matrix - EXPECTED_MATRIX)
        raise ReleaseError(
            f"incomplete PyYAML wheel matrix; missing={missing} unexpected={unexpected}"
        )
    filenames = [spec.filename for spec in specs]
    if len(filenames) != len(set(filenames)):
        raise ReleaseError("duplicate filenames in PyYAML wheel matrix")
    for spec in specs:
        parsed = urllib.parse.urlparse(spec.url)
        if parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org":
            raise ReleaseError(f"non-official PyPI wheel URL: {spec.url}")
        if Path(parsed.path).name != spec.filename:
            raise ReleaseError(f"wheel URL filename mismatch: {spec.url}")
        if (
            f"-{spec.python}-{spec.python}-macosx_" not in spec.filename
            or not spec.filename.endswith(f"_{spec.architecture}.whl")
        ):
            raise ReleaseError(
                f"wheel filename does not match its matrix entry: {spec.filename}"
            )
        if len(spec.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in spec.sha256
        ):
            raise ReleaseError(f"invalid SHA256 for {spec.filename}")


def _download(spec: DependencyWheel, destination: Path) -> None:
    request = urllib.request.Request(
        spec.url,
        headers={"User-Agent": f"{PROJECT_NAME}-release-builder/1"},
    )
    digest = hashlib.sha256()
    temporary = destination.with_name(f".{destination.name}.tmp.{os.getpid()}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.geturl() != spec.url:
                raise ReleaseError(
                    f"unexpected redirect for {spec.filename}: {response.geturl()}"
                )
            with temporary.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
        if digest.hexdigest() != spec.sha256:
            raise ReleaseError(f"SHA256 mismatch for downloaded {spec.filename}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _collect_dependency_wheels(
    destination: Path,
    *,
    provided: Path | None,
    specs: tuple[DependencyWheel, ...] = PYYAML_WHEELS,
    validate_full_matrix: bool = True,
) -> list[Path]:
    if validate_full_matrix:
        _validate_matrix(specs)
    destination.mkdir(parents=True)
    collected = []
    missing = []
    for spec in specs:
        target = destination / spec.filename
        if provided is not None:
            source = provided / spec.filename
            if not source.is_file():
                missing.append(spec.filename)
                continue
            if _sha256_path(source) != spec.sha256:
                raise ReleaseError(f"SHA256 mismatch for provided {spec.filename}")
            shutil.copyfile(source, target)
        else:
            _download(spec, target)
        if _sha256_path(target) != spec.sha256:
            raise ReleaseError(f"SHA256 mismatch after copying {spec.filename}")
        collected.append(target)
    if missing:
        raise ReleaseError(
            "provided wheel directory is missing the locked PyYAML matrix:\n"
            + "\n".join(f"  - {filename}" for filename in missing)
        )
    return collected


def _copy_release_bootstrap(source: Path, staging: Path) -> None:
    copies = {
        "CREDITS.md": "CREDITS.md",
        "LICENSE": "LICENSE",
        "NOTICE": "NOTICE",
        "PROVENANCE.md": "PROVENANCE.md",
        "README.md": "README.md",
        "docs/architecture.md": "docs/architecture.md",
        "docs/compatibility.md": "docs/compatibility.md",
        "docs/install.md": "docs/install.md",
        "docs/provenance/source-commits.tsv": (
            "docs/provenance/source-commits.tsv"
        ),
        "docs/release.md": "docs/release.md",
        "pneu_packaging/__init__.py": "pneu_packaging/__init__.py",
        "pneu_packaging/cli.py": "pneu_packaging/cli.py",
        "pneu_packaging/setup.py": "pneu_packaging/setup.py",
        "scripts/install.sh": "scripts/install.sh",
        "scripts/uninstall.sh": "scripts/uninstall.sh",
    }
    for source_name, target_name in copies.items():
        source_path = source / source_name
        if not source_path.is_file():
            raise ReleaseError(f"release bootstrap file is missing: {source_name}")
        target = staging / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target)
        os.chmod(
            target,
            0o755 if target.suffix == ".sh" else 0o644,
        )

    top_level = (
        "#!/bin/sh\n"
        "set -eu\n"
        'root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
    )
    _atomic_write(
        staging / "install",
        (top_level + 'exec "$root/scripts/install.sh" "$@"\n').encode(),
        0o755,
    )
    _atomic_write(
        staging / "uninstall",
        (top_level + 'exec "$root/scripts/uninstall.sh" "$@"\n').encode(),
        0o755,
    )


def _inner_checksums(staging: Path) -> bytes:
    lines = []
    for path in sorted(staging.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue
        relative = path.relative_to(staging).as_posix()
        lines.append(f"{_sha256_path(path)}  {relative}\n")
    return "".join(lines).encode()


def _assert_outer_allowlist(
    staging: Path,
    project_wheel: Path,
    dependency_wheels: list[Path],
) -> None:
    actual = {
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*")
        if path.is_file()
    }
    expected = {
        *OUTER_STATIC_FILES,
        f"wheels/{project_wheel.name}",
        *(f"wheels/{wheel.name}" for wheel in dependency_wheels),
    }
    if actual != expected:
        raise ReleaseError(
            "release payload allowlist mismatch; "
            f"missing={sorted(expected - actual)} unexpected={sorted(actual - expected)}"
        )
    for name in actual:
        pure = PurePosixPath(name)
        if any(part in FORBIDDEN_COMPONENTS for part in pure.parts):
            raise ReleaseError(f"forbidden runtime path in release payload: {name}")


def _normalized_tar(
    staging: Path,
    artifact: Path,
    *,
    root_name: str,
    source_date_epoch: int,
) -> None:
    uncompressed = artifact.with_suffix("")
    with tarfile.open(uncompressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
        paths = [staging, *sorted(staging.rglob("*"))]
        for path in paths:
            relative = path.relative_to(staging)
            name = root_name if not relative.parts else f"{root_name}/{relative.as_posix()}"
            info = tarfile.TarInfo(name)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = source_date_epoch
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
                continue
            if not path.is_file():
                raise ReleaseError(f"unsupported payload entry type: {path}")
            info.size = path.stat().st_size
            executable = path.stat().st_mode & (
                stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
            info.mode = 0o755 if executable else 0o644
            with path.open("rb") as handle:
                archive.addfile(info, handle)

    temporary = artifact.with_name(f".{artifact.name}.tmp.{os.getpid()}")
    try:
        with uncompressed.open("rb") as source:
            with temporary.open("wb") as raw:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=raw,
                    compresslevel=9,
                    mtime=source_date_epoch,
                ) as compressed:
                    shutil.copyfileobj(source, compressed)
        os.replace(temporary, artifact)
    finally:
        temporary.unlink(missing_ok=True)
        uncompressed.unlink(missing_ok=True)


def build_release(
    *,
    repo: Path,
    output_dir: Path,
    dependency_wheel_dir: Path | None,
    python: Path,
    dependency_specs: tuple[DependencyWheel, ...] = PYYAML_WHEELS,
    validate_full_matrix: bool = True,
) -> BuildResult:
    repo = repo.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    python = python.expanduser().resolve()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ReleaseError(f"build Python is not executable: {python}")
    if dependency_wheel_dir is not None:
        dependency_wheel_dir = dependency_wheel_dir.expanduser().resolve()
        if not dependency_wheel_dir.is_dir():
            raise ReleaseError(
                f"dependency wheel directory does not exist: {dependency_wheel_dir}"
            )

    commit, source_date_epoch = _assert_clean_repo(repo)
    with tempfile.TemporaryDirectory(prefix="pneu-release-") as temporary:
        work = Path(temporary)
        source = work / "source"
        _source_from_commit(repo, commit, source)
        version = _project_version(source)
        toolchain = _build_toolchain(python)

        project_wheel = _build_project_wheel(
            source,
            work / "project-wheel",
            python=python,
            source_date_epoch=source_date_epoch,
            version=version,
        )
        dependencies = _collect_dependency_wheels(
            work / "dependency-wheels",
            provided=dependency_wheel_dir,
            specs=dependency_specs,
            validate_full_matrix=validate_full_matrix,
        )

        staging = work / f"{PROJECT_NAME}-{version}"
        staging.mkdir()
        _copy_release_bootstrap(source, staging)
        wheels = staging / "wheels"
        wheels.mkdir()
        copied_project = wheels / project_wheel.name
        shutil.copyfile(project_wheel, copied_project)
        copied_dependencies = []
        for dependency in dependencies:
            target = wheels / dependency.name
            shutil.copyfile(dependency, target)
            copied_dependencies.append(target)

        metadata = {
            "schema": RELEASE_SCHEMA,
            "project": PROJECT_NAME,
            "version": version,
            "source_commit": commit,
            "source_date_epoch": source_date_epoch,
            "project_wheel": {
                "filename": copied_project.name,
                "sha256": _sha256_path(copied_project),
                "tag": "py3-none-any",
            },
            "dependency": {
                "name": DEPENDENCY_NAME,
                "version": DEPENDENCY_VERSION,
                "wheels": [
                    {
                        "architecture": spec.architecture,
                        "filename": spec.filename,
                        "python": spec.python,
                        "sha256": spec.sha256,
                        "url": spec.url,
                    }
                    for spec in dependency_specs
                ],
            },
            "deterministic_build": {
                "source": "git archive",
                "timestamps": "source commit epoch",
                "uid_gid": 0,
                "scope": "same recorded build toolchain",
                "toolchain": toolchain,
            },
        }
        _atomic_write(
            staging / "BUILD-METADATA.json",
            _json_bytes(metadata),
        )
        _atomic_write(staging / "SHA256SUMS", _inner_checksums(staging))
        _assert_outer_allowlist(staging, copied_project, copied_dependencies)

        output_dir.mkdir(parents=True, exist_ok=True)
        artifact = output_dir / f"{PROJECT_NAME}-{version}-macos.tar.gz"
        _normalized_tar(
            staging,
            artifact,
            root_name=staging.name,
            source_date_epoch=source_date_epoch,
        )

    artifact_hash = _sha256_path(artifact)
    outer = output_dir / "SHA256SUMS"
    _atomic_write(outer, f"{artifact_hash}  {artifact.name}\n".encode())
    return BuildResult(
        artifact=artifact,
        outer_checksums=outer,
        version=version,
        commit=commit,
        sha256=artifact_hash,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the deterministic Roundtable macOS release artifact."
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    parser.add_argument(
        "--dependency-wheel-dir",
        type=Path,
        help="offline directory containing the eight locked PyYAML wheels",
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_release(
            repo=args.repo,
            output_dir=args.output_dir,
            dependency_wheel_dir=args.dependency_wheel_dir,
            python=args.python,
        )
    except (OSError, ReleaseError, zipfile.BadZipFile) as error:
        print(f"build-release: {error}", file=sys.stderr)
        return 1
    print(f"built {result.artifact}")
    print(f"sha256 {result.sha256}")
    print(f"source {result.commit}")
    print(f"checksums {result.outer_checksums}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
