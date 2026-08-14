"""Regression guard for the D15(a) xdist collection blocker.

Every xdist worker collects the suite in its own process and the controller
compares the resulting node ids. A parameter value built at import time from
``uuid.uuid4()``, ``random``, or the clock therefore produces a different node
id per worker, and the whole run aborts with "Different tests were collected"
before a single test body executes. That is exactly what
``tests/test_mailbox_resolver.py`` did until the 1.4 cycle.

The check is the same one xdist makes: collect twice in separate processes and
require the id lists to be identical. See ``handoff/d15a-xdist-verdict.md``.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _collect_node_ids() -> list[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            str(ROOT / "tests"),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    return [line for line in result.stdout.splitlines() if "::" in line]


def test_collected_node_ids_are_identical_across_processes() -> None:
    first = _collect_node_ids()
    second = _collect_node_ids()

    assert first, "collection produced no node ids"
    unstable = sorted(set(first) ^ set(second))
    assert not unstable, (
        "node ids differ between collection processes, which aborts any xdist "
        f"run: {unstable[:10]}"
    )
    assert first == second
