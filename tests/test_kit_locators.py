"""Mutation checks for the test kit's own source locators.

A locator that mis-locates silently is worse than no locator: every assertion
written against it is evaluated over the wrong region and nobody finds out.
These tests mutate the files the real locators are pointed at and require the
locator to react correctly — catching a change inside the target, and *not*
reacting to an unrelated change outside it.

The second half is the part the text-slice locator failed, so each case also
shows what the replaced spelling would have done with the same input.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import _kit as kit


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
LAUNCHER = BIN / "_rtlauncher.py"
GROK_ADAPTER = ROOT / "integrations" / "grok" / "roundtable" / "__init__.py"


def _legacy_slice(source: str, marker: str = "def launch(") -> str:
    """The locator this kit replaced: everything after the marker."""

    return source.split(marker, 1)[1]


# --- unit behaviour on synthetic sources -----------------------------------

SAMPLE = '''\
HEADER = "before"


def target(argument):
    """Docstring."""
    return helper(argument)


TABLE_BELOW = {"leaked": "yes"}


def other():
    return TABLE_BELOW
'''


def test_definition_source_stops_at_the_end_of_the_definition(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text(SAMPLE, encoding="utf-8")

    located = kit.definition_source(module, "target")

    assert located.startswith("def target(argument):")
    assert located.endswith("return helper(argument)")
    assert "TABLE_BELOW" not in located
    assert "def other" not in located
    # The replaced spelling swallowed both.
    assert "TABLE_BELOW" in _legacy_slice(SAMPLE, "def target(")
    assert "def other" in _legacy_slice(SAMPLE, "def target(")


def test_definition_source_names_the_target_when_it_is_missing(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text(SAMPLE, encoding="utf-8")

    with pytest.raises(kit.LocatorError, match="no definition named 'renamed'"):
        kit.definition_source(module, "renamed")


def test_definition_source_refuses_an_ambiguous_bare_name(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text(
        "class A:\n    def run(self):\n        return 1\n\n\n"
        "class B:\n    def run(self):\n        return 2\n",
        encoding="utf-8",
    )

    with pytest.raises(kit.LocatorError, match="ambiguous across A.run, B.run"):
        kit.definition_source(module, "run")

    assert kit.definition_source(module, "A.run").endswith("return 1")
    assert kit.definition_source(module, "B.run").endswith("return 2")


def test_call_source_is_indentation_independent(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text(
        "def outer():\n"
        "    if True:\n"
        "        guard(\n"
        "            first,\n"
        "            second,\n"
        "        )\n",
        encoding="utf-8",
    )

    located = kit.call_source(module, "guard")

    assert located.startswith("guard(")
    assert "first," in located and "second," in located
    assert module.read_text(encoding="utf-8").count(located) == 1


def test_call_source_does_not_conflate_an_attribute_call_with_a_bare_call(tmp_path):
    """Reducing both forms to the trailing attribute name matched the wrong call."""

    module = tmp_path / "sample.py"
    module.write_text(
        "def outer():\n"
        "    client.guard(1)\n"
        "    guard(2)\n",
        encoding="utf-8",
    )

    assert kit.call_source(module, "guard") == "guard(2)"
    assert kit.call_source(module, "client.guard") == "client.guard(1)"
    with pytest.raises(kit.LocatorError, match="no call to 'other.guard'"):
        kit.call_source(module, "other.guard")


def test_definition_source_keeps_the_decorators(tmp_path):
    """A decorator is part of the definition and is often the policy."""

    module = tmp_path / "sample.py"
    module.write_text(
        "import pytest\n\n\n"
        "@pytest.mark.integration\n"
        "@pytest.mark.slow\n"
        "def target():\n"
        "    return 1\n\n\n"
        "class Holder:\n"
        "    @property\n"
        "    def value(self):\n"
        "        return 2\n",
        encoding="utf-8",
    )

    located = kit.definition_source(module, "target")
    assert located.startswith("@pytest.mark.integration\n@pytest.mark.slow\ndef target(")
    assert "import pytest" not in located

    method = kit.definition_source(module, "Holder.value")
    assert method.startswith("@property\ndef value(")

    assert kit.definition_source(
        module, "target", include_decorators=False
    ).startswith("def target(")


def test_call_source_refuses_zero_and_multiple_matches(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text(
        "def a():\n    guard(1)\n\n\ndef b():\n    guard(2)\n",
        encoding="utf-8",
    )

    with pytest.raises(kit.LocatorError, match="2 calls to 'guard'"):
        kit.call_source(module, "guard")
    with pytest.raises(kit.LocatorError, match="no call to 'missing'"):
        kit.call_source(module, "missing")
    assert kit.call_source(module, "guard", within="a") == "guard(1)"


# --- mutation checks against the real files the locators are aimed at -------


def _inject_first_statement(path: Path, function: str, statement: str) -> str:
    """Return ``path``'s source with ``statement`` as ``function``'s first line.

    Inserting after the first newline of the definition breaks a multi-line
    signature, so the insertion point comes from the AST.
    """

    text = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.FunctionDef) and item.name == function
    )
    first = node.body[0]
    lines = text.splitlines(keepends=True)
    indent = " " * first.col_offset
    lines.insert(first.lineno - 1, f"{indent}{statement}\n")
    return "".join(lines)


def _mutated_launcher(tmp_path: Path, *, inside: bool) -> Path:
    """Copy ``_rtlauncher.py`` with a marker added inside or below ``launch``."""

    source = LAUNCHER.read_text(encoding="utf-8")
    body = kit.definition_source(LAUNCHER, "launch")
    assert source.count(body) == 1
    if inside:
        # Insert the marker as the first statement of the body, immediately
        # after the signature line.
        mutated = source.replace(
            body,
            body.replace("\n", "\n    grok_adapter_bin()\n", 1),
            1,
        )
    else:
        mutated = source.replace(
            body,
            body + '\n\n\nGROK_TABLE_BELOW = {"bin": grok_adapter_bin}\n',
            1,
        )
    target = tmp_path / "_rtlauncher.py"
    target.write_text(mutated, encoding="utf-8")
    return target


def test_locator_catches_the_marker_moving_into_the_launch_body(tmp_path):
    mutated = _mutated_launcher(tmp_path, inside=True)

    located = kit.definition_source(mutated, "launch")

    assert "grok_adapter_bin" in located
    assert "grok_adapter_bin" in _legacy_slice(
        mutated.read_text(encoding="utf-8")
    )


def test_locator_ignores_a_table_added_below_the_launch_body(tmp_path):
    """This is the regression the text slice could not survive.

    ``_rtharness.py`` is expected to move harness tables around; a table
    placed after ``launch()`` is not part of the seat path and must not fail
    the test that pins it.
    """

    mutated = _mutated_launcher(tmp_path, inside=False)

    located = kit.definition_source(mutated, "launch")

    assert "grok_adapter_bin" not in located
    assert "GROK_TABLE_BELOW" not in located
    # The replaced spelling raised a false alarm on exactly this input.
    assert "grok_adapter_bin" in _legacy_slice(
        mutated.read_text(encoding="utf-8")
    )


def test_seat_path_locator_catches_the_marker_one_call_deeper(tmp_path):
    """The claim is about launch() *and what it calls*, not launch()'s body.

    This is the mutation that the body-only check could not see: move the
    reference into a module-level helper and call the helper from launch().
    """

    target = tmp_path / "_rtlauncher.py"
    target.write_text(
        _inject_first_statement(LAUNCHER, "choose_launch_cwd", "grok_adapter_bin()"),
        encoding="utf-8",
    )

    assert "choose_launch_cwd" in kit.reachable_definitions(target, "launch")
    assert "grok_adapter_bin" in kit.reachable_source(target, "launch")
    # The body-only spelling stays green on exactly this input.
    assert "grok_adapter_bin" not in kit.definition_source(target, "launch")


def test_seat_path_locator_stops_at_the_module_boundary(tmp_path):
    module = tmp_path / "sample.py"
    module.write_text(
        "def entry():\n    helper()\n    unrelated_import.run()\n\n\n"
        "def helper():\n    deeper()\n\n\n"
        "def deeper():\n    return 1\n\n\n"
        "def never_called():\n    return 2\n",
        encoding="utf-8",
    )

    reached = kit.reachable_definitions(module, "entry")

    assert reached == ["entry", "helper", "deeper"]
    assert "never_called" not in kit.reachable_source(module, "entry")
    with pytest.raises(kit.LocatorError, match="no module-level function named"):
        kit.reachable_definitions(module, "missing")


def test_call_locator_survives_reindenting_the_grok_lease_check(tmp_path):
    source = GROK_ADAPTER.read_text(encoding="utf-8")
    pinned_by_hand = """load_validated_lease(
                self.project_root,
                self.agent_id,
                self.session_id,
                self.revision,
            )"""
    assert source.count(pinned_by_hand) == 1

    reindented = source.replace(
        pinned_by_hand,
        pinned_by_hand.replace("\n            ", "\n        "),
        1,
    )
    target = tmp_path / "grok_adapter.py"
    target.write_text(reindented, encoding="utf-8")

    derived = kit.call_source(target, "load_validated_lease")

    assert reindented.count(derived) == 1
    assert "self.project_root" in derived
    assert "self.revision" in derived
    # The hand-copied needle is what breaks under a reindent.
    assert reindented.count(pinned_by_hand) == 0
