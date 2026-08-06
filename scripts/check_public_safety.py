#!/usr/bin/env python3
"""Fail when a public checkout contains local runtime state or likely secrets."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_SEGMENTS = {
    ".claude",
    ".pytest_cache",
    ".roundtable",
    ".runtime",
    "__pycache__",
    "inbox",
    "layout-locks",
    "locks",
    "mail",
    "messages",
    "skills/vendor",
}
FORBIDDEN_NAMES = {
    "projects.yaml",
    "projects.yaml.lock",
    "runtime.json",
}

CONTENT_RULES = (
    (
        "personal absolute path",
        re.compile(rf"(?:/{'Users'}/|/{'home'}/)[^/\s]+/"),
    ),
    (
        "local username",
        re.compile("haiyang" + "yu", re.IGNORECASE),
    ),
    (
        "private Claude session URL",
        re.compile(r"claude\.ai/code/" + "session_", re.IGNORECASE),
    ),
    (
        "private key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ),
    (
        "OpenAI-style secret",
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    ),
    (
        "AWS access key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "GitHub token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ),
    (
        "Slack token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
)

METADATA_RULES = (
    (
        "private Claude session URL",
        re.compile(r"claude\.ai/code/" + "session_", re.IGNORECASE),
    ),
)
RELEASE_SURFACE_PREFIXES = ("bin/", "pneu_packaging/")
RELEASE_FORBIDDEN_TOKENS = (
    "RT_MIGRATION_" + "FAILPOINT",
)


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed with {proc.returncode}: "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def forbidden_path(path: str) -> str | None:
    pure = PurePosixPath(path)
    parts = pure.parts
    if any(part.startswith("bin.bak-") for part in parts):
        return "pre-period backup"
    if pure.name.endswith(".pyc"):
        return "compiled Python artifact"
    if pure.name in FORBIDDEN_NAMES:
        return "local runtime state"
    for segment in FORBIDDEN_SEGMENTS:
        segment_parts = PurePosixPath(segment).parts
        width = len(segment_parts)
        if any(
            parts[index : index + width] == segment_parts
            for index in range(len(parts) - width + 1)
        ):
            return "local runtime state"
    return None


def tracked_paths() -> list[str]:
    return [line for line in git("ls-files").splitlines() if line]


def historical_paths() -> list[str]:
    paths = []
    for line in git("rev-list", "--objects", "--all").splitlines():
        _, separator, path = line.partition(" ")
        if separator:
            paths.append(path)
    return paths


def scan_text(path: Path, text: str) -> list[str]:
    errors = []
    for label, pattern in CONTENT_RULES:
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{path.relative_to(ROOT)}:{line}: {label}")
    return errors


def scan_worktree(paths: list[str]) -> list[str]:
    errors = []
    for relative in paths:
        path = ROOT / relative
        try:
            payload = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue
        if b"\0" in payload:
            continue
        text = payload.decode("utf-8", errors="replace")
        errors.extend(scan_text(path, text))
        if relative.startswith(RELEASE_SURFACE_PREFIXES):
            for token in RELEASE_FORBIDDEN_TOKENS:
                if token in text:
                    errors.append(
                        f"{relative}: production-active test fault injection"
                    )
    return errors


def scan_metadata() -> list[str]:
    text = git("log", "--all", "--format=%H%n%B")
    errors = []
    for label, pattern in METADATA_RULES:
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"git-history:{line}: {label}")
    return errors


def main() -> int:
    errors = []
    tracked = tracked_paths()

    for path in tracked:
        reason = forbidden_path(path)
        if reason:
            errors.append(f"{path}: forbidden tracked path ({reason})")

    for path in historical_paths():
        reason = forbidden_path(path)
        if reason:
            errors.append(f"{path}: forbidden historical path ({reason})")

    errors.extend(scan_worktree(tracked))
    errors.extend(scan_metadata())

    if errors:
        print("public-safety check failed:", file=sys.stderr)
        for error in sorted(set(errors)):
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(
        f"public-safety check passed "
        f"({len(tracked)} tracked files, full reachable history)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
