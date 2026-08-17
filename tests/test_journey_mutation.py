"""Mutation guards for the journey tier.

A journey test that stays green when the guard it claims to pin is deleted is
not evidence of anything. Each mutation below removes exactly one load-bearing
condition from a private copy of ``bin/`` and asserts that the journey tests
naming that condition turn red.

The private copy mirrors the checkout layout (``<root>/bin`` and
``<root>/tests``) so the copied journey modules resolve ``ROOT`` to the copy and
exercise the mutated sources, never the checkout.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
JOURNEY_MODULES = ("test_open_journey.py", "test_journey_core.py")


@dataclass(frozen=True)
class Mutation:
    slug: str
    filename: str
    needle: str
    replacement: str
    selector: str


MUTATIONS = (
    Mutation(
        slug="print-skips-the-launchable-seat-gate",
        filename="rt-worktree",
        needle=(
            '    if selection.kind != "print":\n'
            "        _require_launchable_seat(target, agent_id)"
        ),
        replacement=(
            "    if True:\n"
            "        _require_launchable_seat(target, agent_id)"
        ),
        selector="print_fallback_prints_over_an_active_seat",
    ),
    Mutation(
        slug="printing-is-not-a-launch",
        filename="_rtsurface.py",
        needle="        return SurfaceLaunch(False, printed)",
        replacement="        return SurfaceLaunch(True, printed)",
        selector="print_fallback_prints_over_an_active_seat or ambient_fallback_prints",
    ),
    Mutation(
        slug="ambient-detection-falls-back-to-print",
        filename="_rtsurface.py",
        needle='    return SurfaceSelection("print", "fallback")',
        replacement='    return SurfaceSelection("herdr", "fallback")',
        selector="ambient_fallback_prints",
    ),
    Mutation(
        slug="down-arrow-moves-the-card-cursor",
        filename="pneu",
        needle='            return {"[A": "up", "[B": "down"}.get(suffix, "unknown")',
        replacement='            return {"[A": "up"}.get(suffix, "unknown")',
        selector="launcher_navigation_journey",
    ),
    Mutation(
        slug="watcher-claims-the-wake-slot",
        filename="rt-wait-inbox",
        needle="                watcher_pid=watcher_pid,\n            )\n        except FenceRejected as error:",
        replacement="                watcher_pid=None,\n            )\n        except FenceRejected as error:",
        selector="seat_open_journey",
    ),
    Mutation(
        slug="watcher-wakes-on-new-mail",
        filename="rt-wait-inbox",
        needle="            mail = candidate_mail",
        replacement="            mail = []",
        selector="mail_journey",
    ),
    Mutation(
        slug="ack-archives-out-of-new",
        filename="rt-ack",
        needle="    source.unlink()\n    fsync_directory(new_dir)",
        replacement="    fsync_directory(new_dir)",
        selector="mail_journey",
    ),
    Mutation(
        slug="ack-returns-a-quiet-receipt",
        filename="rt-ack",
        needle='    needs_receipt = "cur" not in paths_by_lifecycle',
        replacement="    needs_receipt = False",
        selector="mail_journey",
    ),
)


def _private_copy(root: Path) -> Path:
    for directory in ("bin", "templates", "skills"):
        shutil.copytree(
            ROOT / directory,
            root / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            symlinks=True,
        )
    tests = root / "tests"
    tests.mkdir()
    for name in ("conftest.py", *JOURNEY_MODULES):
        shutil.copy2(ROOT / "tests" / name, tests / name)
    shutil.copytree(
        ROOT / "tests" / "_kit",
        tests / "_kit",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return root


def _run_journeys(root: Path, selector: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "bin")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *[str(root / "tests" / name) for name in JOURNEY_MODULES],
            "-k",
            selector,
        ],
        cwd=root,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _summary_line(stdout: str) -> str:
    """Return pytest's final counts line, or an empty string."""

    for line in reversed(stdout.splitlines()):
        if " in " in line and ("passed" in line or "failed" in line or "error" in line):
            return line
    return ""


def _failed_tests(stdout: str) -> list[str]:
    """Return the node ids pytest reported as FAILED."""

    return [
        line.split(" ", 1)[1].split(" - ", 1)[0].strip()
        for line in stdout.splitlines()
        if line.startswith("FAILED ")
    ]


def test_private_journey_copy_is_green_before_any_mutation(tmp_path):
    """The harness itself must be trustworthy before a red run means anything."""

    root = _private_copy(tmp_path / "baseline")
    result = _run_journeys(root, "journey")
    assert result.returncode == 0, result.stdout
    assert "no tests ran" not in result.stdout


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda mutation: mutation.slug)
def test_journey_guard_mutations_turn_the_private_copy_red(tmp_path, mutation):
    root = _private_copy(tmp_path / mutation.slug)
    path = root / "bin" / mutation.filename
    source = path.read_text(encoding="utf-8")
    assert source.count(mutation.needle) == 1, mutation.slug
    path.write_text(
        source.replace(mutation.needle, mutation.replacement, 1),
        encoding="utf-8",
    )

    result = _run_journeys(root, mutation.selector)

    # A nonzero exit is not evidence that the *journey* caught the mutation: a
    # mutant that broke the syntax of the file, or one that only made
    # collection fail, would exit nonzero while pinning nothing. Require a
    # real test failure and no collection error.
    assert "no tests ran" not in result.stdout, mutation.slug
    assert result.returncode != 0, f"mutation {mutation.slug} survived:\n{result.stdout}"
    assert "errors" not in _summary_line(result.stdout), (
        f"mutation {mutation.slug} produced a collection/import error rather "
        f"than a journey failure:\n{result.stdout}"
    )
    failed = _failed_tests(result.stdout)
    assert failed, (
        f"mutation {mutation.slug} exited nonzero without failing a journey "
        f"test:\n{result.stdout}"
    )
