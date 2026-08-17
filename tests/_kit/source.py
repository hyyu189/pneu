"""AST locators for tests that assert on production source text.

A test that slices source with ``str.split`` or a hand-copied indented needle
is not pinning what it claims to pin. The live case this module replaces was
``tests/test_grok_adapter.py``::

    launch_source = launcher_source.split("def launch(", 1)[1]

That is *everything after* the ``def launch(`` line — to end of file, not the
function body — so it silently absorbs every definition placed below
``launch()``. Assertions written against it are evaluated over the wrong
region, and the region changes whenever unrelated code moves.

Everything here locates by syntax and fails loudly. A name that does not
resolve, or resolves more than once, is an error with the candidates named:
a locator that cannot find its target must never quietly return a bigger or
smaller slice instead.
"""

from __future__ import annotations

import ast
from pathlib import Path


class LocatorError(AssertionError):
    """A locator could not resolve exactly one target."""


def _parse(path: Path) -> tuple[str, ast.Module]:
    text = Path(path).read_text(encoding="utf-8")
    return text, ast.parse(text, filename=str(path))


def _segment(text: str, node: ast.AST, path: Path) -> str:
    segment = ast.get_source_segment(text, node)
    if segment is None:  # pragma: no cover - only on position-less nodes
        raise LocatorError(f"{path}: node has no source segment: {ast.dump(node)}")
    return segment


_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _iter_definitions(tree: ast.AST):
    """Yield ``(qualname, node)`` for every function and class in the tree."""

    def walk(node: ast.AST, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (*_FUNCTION_NODES, ast.ClassDef)):
                qualname = f"{prefix}{child.name}"
                yield qualname, child
                yield from walk(child, f"{qualname}.")
            else:
                yield from walk(child, prefix)

    yield from walk(tree, "")


def _resolve_one(path: Path, tree: ast.Module, name: str) -> ast.AST:
    """Resolve ``name`` as a bare name or a dotted qualname, exactly once."""

    definitions = list(_iter_definitions(tree))
    exact = [node for qualname, node in definitions if qualname == name]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise LocatorError(
            f"{path}: {name!r} is defined {len(exact)} times; "
            "use a dotted qualname to disambiguate"
        )
    bare = [
        (qualname, node)
        for qualname, node in definitions
        if qualname.rsplit(".", 1)[-1] == name
    ]
    if len(bare) == 1:
        return bare[0][1]
    if not bare:
        available = sorted(qualname for qualname, _node in definitions)
        raise LocatorError(
            f"{path}: no definition named {name!r}. Defined here: "
            + ", ".join(available[:20])
            + (" ..." if len(available) > 20 else "")
        )
    raise LocatorError(
        f"{path}: {name!r} is ambiguous across "
        + ", ".join(qualname for qualname, _node in bare)
    )


def definition_source(path: Path | str, name: str) -> str:
    """Return the exact source of one function or class, and nothing else.

    ``name`` is a bare name or a dotted qualname (``Class.method``). Unlike a
    text slice this stops at the end of the definition, so code added below it
    cannot leak into the assertion.
    """

    path = Path(path)
    text, tree = _parse(path)
    return _segment(text, _resolve_one(path, tree, name), path)


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def call_sources(
    path: Path | str,
    callee: str,
    *,
    within: str | None = None,
) -> list[str]:
    """Return the exact source of every call to ``callee``, in source order.

    ``within`` restricts the search to one function or class by name, so a
    caller can say "the lease check inside this method" without pinning the
    surrounding indentation.
    """

    path = Path(path)
    text, tree = _parse(path)
    scope: ast.AST = tree if within is None else _resolve_one(path, tree, within)
    found = [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call) and _callee_name(node) == callee
    ]
    found.sort(key=lambda node: (node.lineno, node.col_offset))
    return [_segment(text, node, path) for node in found]


def call_source(
    path: Path | str,
    callee: str,
    *,
    within: str | None = None,
) -> str:
    """Return the exact source of the single call to ``callee``.

    This is the indentation-independent replacement for a hand-copied
    multi-line needle: the text comes from the file being mutated, so
    reindenting the target cannot turn a safety test into a confusing
    ``count != 1`` failure.
    """

    found = call_sources(path, callee, within=within)
    location = f"{path}" + (f" within {within!r}" if within else "")
    if not found:
        raise LocatorError(f"{location}: no call to {callee!r}")
    if len(found) > 1:
        raise LocatorError(
            f"{location}: {len(found)} calls to {callee!r}; "
            "narrow the search with within=..."
        )
    return found[0]


def defined_names(path: Path | str) -> set[str]:
    """Return every function and class qualname defined in ``path``."""

    path = Path(path)
    _text, tree = _parse(path)
    return {qualname for qualname, _node in _iter_definitions(tree)}


def called_names(path: Path | str) -> set[str]:
    """Return every called name in ``path``, by bare function or attribute."""

    path = Path(path)
    _text, tree = _parse(path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _callee_name(node)
            if name is not None:
                names.add(name)
    return names
