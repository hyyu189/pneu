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


def definition_source(
    path: Path | str,
    name: str,
    *,
    include_decorators: bool = True,
) -> str:
    """Return the exact source of one function or class, and nothing else.

    ``name`` is a bare name or a dotted qualname (``Class.method``). Unlike a
    text slice this stops at the end of the definition, so code added below it
    cannot leak into the assertion.

    Decorators are part of the definition and are frequently the
    policy-bearing part of it, so they are included by default;
    ``ast.get_source_segment`` alone starts at ``def`` and drops them.
    """

    path = Path(path)
    text, tree = _parse(path)
    node = _resolve_one(path, tree, name)
    segment = _segment(text, node, path)
    decorators = getattr(node, "decorator_list", []) if include_decorators else []
    if not decorators:
        return segment
    lines = text.splitlines(keepends=True)
    first = min(decorator.lineno for decorator in decorators)
    indent = " " * node.col_offset
    prefix = "".join(
        line[len(indent) :] if line.startswith(indent) else line
        for line in lines[first - 1 : node.lineno - 1]
    )
    return prefix + segment


def _dotted(node: ast.expr) -> str | None:
    """Render ``a.b.c`` / ``name`` as a dotted string, or None if it is neither."""

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return None if prefix is None else f"{prefix}.{node.attr}"
    return None


def _callee_matches(node: ast.Call, callee: str) -> bool:
    """Match a call by exact identity, not by its trailing attribute name.

    A bare ``load_validated_lease(...)`` and an unrelated
    ``client.load_validated_lease(...)`` are different callees. Reducing both
    to the trailing ``attr`` conflated them, so ``call_source`` could report
    an ambiguity — or silently pick the wrong call in the single-match case —
    for a name it was never asked about. A dotted ``callee`` matches the
    attribute form explicitly.
    """

    func = node.func
    if "." in callee:
        return _dotted(func) == callee
    return isinstance(func, ast.Name) and func.id == callee


def call_sources(
    path: Path | str,
    callee: str,
    *,
    within: str | None = None,
) -> list[str]:
    """Return the exact source of every call to ``callee``, in source order.

    A bare ``callee`` matches only a bare-name call. Pass a dotted name
    (``client.send``) to match an attribute call; the two are never conflated.

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
        if isinstance(node, ast.Call) and _callee_matches(node, callee)
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


def reachable_definitions(path: Path | str, entry: str) -> list[str]:
    """Return ``entry`` plus every module-level function it can reach, in order.

    A test that says "this code path never touches X" means the entry point
    *and what it calls*. Checking only the entry's own body lets the reference
    move one call deeper and stay green, which is a weaker claim than the test
    name makes. Resolution is by module-level name, so it does not follow
    calls into imported modules or through indirection.
    """

    path = Path(path)
    text, tree = _parse(path)
    definitions = {
        node.name: node
        for node in tree.body
        if isinstance(node, _FUNCTION_NODES)
    }
    if entry not in definitions:
        raise LocatorError(f"{path}: no module-level function named {entry!r}")
    seen: set[str] = set()
    order: list[str] = []
    stack = [entry]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        order.append(name)
        for node in ast.walk(definitions[name]):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            callee = func.id if isinstance(func, ast.Name) else None
            if callee in definitions:
                stack.append(callee)
    return order


def reachable_source(path: Path | str, entry: str) -> str:
    """Return the concatenated source of :func:`reachable_definitions`."""

    path = Path(path)
    return "\n".join(
        definition_source(path, name) for name in reachable_definitions(path, entry)
    )


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
