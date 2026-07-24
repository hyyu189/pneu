#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_root=$(CDPATH= cd -- "$script_dir/.." && pwd)

supported_python() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)' >/dev/null 2>&1
}

if [ -n "${ROUNDTABLE_BOOTSTRAP_PYTHON:-}" ]; then
  if ! bootstrap_python=$(command -v "$ROUNDTABLE_BOOTSTRAP_PYTHON" 2>/dev/null); then
    echo "roundtable-uninstall: CPython 3.11 through 3.14 is required; not found: $ROUNDTABLE_BOOTSTRAP_PYTHON" >&2
    exit 1
  fi
  if ! supported_python "$bootstrap_python"; then
    echo "roundtable-uninstall: $bootstrap_python must be CPython 3.11 through 3.14" >&2
    exit 1
  fi
else
  bootstrap_python=
  # Prefer an already-activated environment before scanning PATH by version
  # name, matching the installer's discovery order. Uninstall never builds, so
  # no source-capability check applies here.
  for candidate in \
    "${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}" \
    "${CONDA_PREFIX:+$CONDA_PREFIX/bin/python}" \
    python3.14 python3.13 python3.12 python3.11 python3; do
    [ -n "$candidate" ] || continue
    if candidate_path=$(command -v "$candidate" 2>/dev/null) && supported_python "$candidate_path"; then
      bootstrap_python=$candidate_path
      break
    fi
  done
  if [ -z "$bootstrap_python" ]; then
    echo "roundtable-uninstall: CPython 3.11 through 3.14 is required; no supported interpreter was found on PATH" >&2
    exit 1
  fi
fi

PYTHONPATH="$source_root${PYTHONPATH:+:$PYTHONPATH}" \
  exec "$bootstrap_python" -m roundtable_packaging.cli uninstall "$@"
