"""Mutation checks for the derived architectural-fitness consumer list.

The point of deriving the list is that a source nobody remembered to add is
still checked. That claim is only worth something if it is demonstrated, so
these tests build synthetic trees containing exactly the sources a
hand-maintained list would have missed and require the derivation to find
them.
"""

from __future__ import annotations

from pathlib import Path

from _kit import consumers


def _tree(root: Path, files: dict[str, str]) -> Path:
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        if not path.suffix:
            path.chmod(0o755)
    return root


def test_discovery_finds_new_sources_nobody_registered(tmp_path):
    root = _tree(
        tmp_path,
        {
            "bin/rt-newcomer": "#!/usr/bin/env python3\nvalue = 1\n",
            "bin/_rthelper.py": "value = 2\n",
            "integrations/newharness/roundtable/__init__.py": "value = 3\n",
            "pneu_packaging/extra.py": "value = 4\n",
        },
    )

    found = {str(path.relative_to(root)) for path in consumers.production_sources(root)}

    assert found == {
        "bin/rt-newcomer",
        "bin/_rthelper.py",
        "integrations/newharness/roundtable/__init__.py",
        "pneu_packaging/extra.py",
    }


def test_discovery_finds_a_pyw_tool(tmp_path):
    """``.pyw`` is Python; an extension-only rule made a shipped tool invisible."""

    root = _tree(tmp_path, {"bin/tool.pyw": "value = 1\n"})

    found = {str(path.relative_to(root)) for path in consumers.production_sources(root)}

    assert found == {"bin/tool.pyw"}


def test_discovery_skips_non_python_and_developer_trees(tmp_path):
    root = _tree(
        tmp_path,
        {
            "bin/rt-shell": "#!/bin/sh\nexit 0\n",
            "bin/notes.md": "not code\n",
            "scripts/dev_tool.py": "value = 1\n",
            "tests/test_thing.py": "value = 2\n",
            "bin/real.py": "value = 3\n",
        },
    )
    (root / "bin" / "rt-shell").chmod(0o755)

    found = {str(path.relative_to(root)) for path in consumers.production_sources(root)}

    assert found == {"bin/real.py"}


def test_discovery_counts_a_symlinked_alias_once(tmp_path):
    root = _tree(tmp_path, {"bin/pneu": "#!/usr/bin/env python3\nvalue = 1\n"})
    (root / "bin" / "roundtable").symlink_to("pneu")

    found = [str(path.relative_to(root)) for path in consumers.production_sources(root)]

    assert found == ["bin/pneu"]


def test_facts_classify_a_new_unlocked_maildir_consumer(tmp_path):
    root = _tree(
        tmp_path,
        {
            "bin/rt-newcomer": (
                "#!/usr/bin/env python3\n"
                "from _rtlib import resolve_project_mailbox_checked\n"
                "mailbox = resolve_project_mailbox_checked(root)\n"
                "target = mailbox.inbox_dir / 'agent' / 'new'\n"
            ),
        },
    )

    facts = consumers.all_facts(root)

    assert len(facts) == 1
    newcomer = facts[0]
    assert newcomer.relative == "bin/rt-newcomer"
    assert newcomer.touches_maildir
    assert newcomer.calls_raw_resolver
    assert not newcomer.calls_locked_resolver
    # The invariant the real suite asserts would reject this source.
    assert newcomer in consumers.maildir_consumers(root)


def test_facts_accept_a_new_locked_maildir_consumer(tmp_path):
    root = _tree(
        tmp_path,
        {
            "bin/rt-newcomer": (
                "#!/usr/bin/env python3\n"
                "from _rtlib import locked_project_mailbox_checked\n"
                "with locked_project_mailbox_checked(root) as mailbox:\n"
                "    target = mailbox.inbox_dir\n"
            ),
        },
    )

    newcomer = consumers.all_facts(root)[0]

    assert newcomer.touches_maildir
    assert newcomer.calls_locked_resolver
    assert not newcomer.calls_raw_resolver


def test_a_source_that_ignores_the_mailbox_is_not_a_consumer(tmp_path):
    root = _tree(tmp_path, {"bin/rt-quiet": "#!/usr/bin/env python3\nvalue = 1\n"})

    assert consumers.all_facts(root)[0].touches_maildir is False
    assert consumers.maildir_consumers(root) == []
