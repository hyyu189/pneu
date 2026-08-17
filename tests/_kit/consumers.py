"""Derived inventory of the production sources an architectural test covers.

The mailbox-layout fitness tests used to name their consumers by hand. A
hand-maintained list cannot fail for the one reason that matters: a new tool
that reaches the maildir and is never added to it is simply not checked, and
nothing says so. Both ``integrations/`` adapters were in exactly that position
— absent from the layout-path list while being the two files most likely to
drift.

So the universe is discovered from the tree, and the only hand-written part is
a small ledger of exemptions, each with a reason. The ledger is itself
checked: an exemption naming a path that no longer exists, or a path that no
longer needs exempting, fails.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

#: Where shipped Python lives. ``scripts/`` is developer tooling and
#: ``tests/`` is this suite, so neither is a production mailbox consumer.
PRODUCTION_TREES = ("bin", "integrations", "pneu_packaging")

RAW_RESOLVERS = frozenset(
    {"resolve_project_mailbox", "resolve_project_mailbox_checked"}
)
LOCKED_RESOLVERS = frozenset(
    {"locked_project_mailbox", "locked_project_mailbox_checked"}
)
#: Attribute names that only a resolved mailbox exposes. Touching one of these
#: means the caller has reached the maildir itself, not merely project identity.
MAILDIR_ATTRIBUTES = frozenset({"inbox_dir", "messages_dir", "locks_dir"})


def _is_python_source(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    if "__pycache__" in path.parts:
        return False
    if path.suffix == ".py":
        return True
    if path.suffix:
        return False
    try:
        first_line = path.read_bytes().split(b"\n", 1)[0]
    except OSError:  # pragma: no cover - unreadable file in a checkout
        return False
    return first_line.startswith(b"#!") and b"python" in first_line


def production_sources(root: Path | None = None) -> list[Path]:
    """Every shipped Python source, de-duplicated across symlinked names.

    ``bin/roundtable`` is a compatibility symlink to ``bin/pneu``; counting it
    twice would double every finding it produces.

    ``root`` is injectable so the discovery rule can be tested against a
    synthetic tree rather than only against the checkout it ships with.
    """

    base = ROOT if root is None else Path(root)
    seen: set[Path] = set()
    found: list[Path] = []
    for tree in PRODUCTION_TREES:
        directory = base / tree
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not _is_python_source(path):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(path)
    return found


@dataclass(frozen=True)
class SourceFacts:
    """What one source does with the project mailbox."""

    path: Path
    root: Path
    calls_raw_resolver: bool
    calls_locked_resolver: bool
    touches_maildir: bool

    @property
    def relative(self) -> str:
        return str(self.path.relative_to(self.root))


def source_facts(path: Path, root: Path | None = None) -> SourceFacts:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
    called: set[str] = set()
    attributes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name):
                called.add(function.id)
            elif isinstance(function, ast.Attribute):
                called.add(function.attr)
        elif isinstance(node, ast.Attribute) and node.attr in MAILDIR_ATTRIBUTES:
            attributes.add(node.attr)
    return SourceFacts(
        path=Path(path),
        root=ROOT if root is None else Path(root),
        calls_raw_resolver=bool(called & RAW_RESOLVERS),
        calls_locked_resolver=bool(called & LOCKED_RESOLVERS),
        touches_maildir=bool(attributes),
    )


def all_facts(root: Path | None = None) -> list[SourceFacts]:
    return [
        source_facts(path, root=root) for path in production_sources(root=root)
    ]


def maildir_consumers(root: Path | None = None) -> list[SourceFacts]:
    """Sources that reach the maildir itself, however they resolved it."""

    return [facts for facts in all_facts(root=root) if facts.touches_maildir]
