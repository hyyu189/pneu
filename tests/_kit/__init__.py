"""Shared scaffolding for the test suite.

Two concerns live here, both of them facts that were previously written down
once per test module and drifted:

- ``source`` — AST locators for tests that assert on production source text.
- ``projects`` — the ``.roundtable`` project fixture that 13 modules each
  spelled out by hand.

Nothing in this package imports production code, so it is safe to import from
any test module regardless of the environment fixtures that module installs.
"""

from __future__ import annotations

from . import consumers
from .projects import (
    CLAUDE,
    CODEX,
    GROK,
    HERMES,
    OPENCLAW,
    PROJECT_DOT,
    PROJECT_DOT_BARE,
    SCHEMA,
    Seat,
    agents_document,
    write_project,
)
from .source import (
    LocatorError,
    call_source,
    call_sources,
    called_names,
    defined_names,
    definition_source,
)

__all__ = [
    "CLAUDE",
    "consumers",
    "CODEX",
    "GROK",
    "HERMES",
    "LocatorError",
    "OPENCLAW",
    "PROJECT_DOT",
    "PROJECT_DOT_BARE",
    "SCHEMA",
    "Seat",
    "agents_document",
    "call_source",
    "call_sources",
    "called_names",
    "defined_names",
    "definition_source",
    "write_project",
]
