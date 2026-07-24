#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_root=$(CDPATH= cd -- "$script_dir/.." && pwd)

supported_python() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)' >/dev/null 2>&1
}

can_build_from_source() {
  "$1" -c 'import setuptools.build_meta' >/dev/null 2>&1
}

# Resolve the install mode before interpreter discovery. A source build runs
# the wheel build backend, so its bootstrap interpreter must provide
# setuptools; a wheel-mode install (the release artifact ships a wheels/
# directory) never builds and must not reject an interpreter that merely lacks
# setuptools.
mode=source
for argument in "$@"; do
  case "$argument" in
    --wheel-dir|--wheel-dir=*)
      mode=wheel
      ;;
    --source-root|--source-root=*)
      mode=explicit
      ;;
  esac
done

if [ "$mode" = wheel ]; then
  build_from_source=no
elif [ "$mode" = explicit ]; then
  build_from_source=yes
elif [ -d "$source_root/wheels" ]; then
  build_from_source=no
else
  build_from_source=yes
fi

# Record a candidate if it is a supported CPython that can satisfy the current
# install mode. Source builds additionally require setuptools; a candidate that
# cannot build is skipped with a diagnostic so discovery continues.
try_bootstrap_candidate() {
  candidate_path=$(command -v "$1" 2>/dev/null) || return 1
  supported_python "$candidate_path" || return 1
  if [ "$build_from_source" = yes ] && ! can_build_from_source "$candidate_path"; then
    echo "roundtable-install: skipping $candidate_path: cannot build from source (setuptools unavailable)" >&2
    return 1
  fi
  bootstrap_python=$candidate_path
  return 0
}

if [ -n "${ROUNDTABLE_BOOTSTRAP_PYTHON:-}" ]; then
  if ! bootstrap_python=$(command -v "$ROUNDTABLE_BOOTSTRAP_PYTHON" 2>/dev/null); then
    echo "roundtable-install: CPython 3.11 through 3.14 is required; not found: $ROUNDTABLE_BOOTSTRAP_PYTHON" >&2
    echo "set ROUNDTABLE_BOOTSTRAP_PYTHON=/absolute/path/to/python3" >&2
    exit 1
  fi
  if ! supported_python "$bootstrap_python"; then
    echo "roundtable-install: $bootstrap_python must be CPython 3.11 through 3.14" >&2
    echo "set ROUNDTABLE_BOOTSTRAP_PYTHON=/absolute/path/to/a/supported/python3" >&2
    exit 1
  fi
  if [ "$build_from_source" = yes ] && ! can_build_from_source "$bootstrap_python"; then
    echo "roundtable-install: $bootstrap_python cannot build from source (setuptools unavailable)" >&2
    echo "install setuptools into it, or set ROUNDTABLE_BOOTSTRAP_PYTHON to a python3 that can build" >&2
    exit 1
  fi
else
  bootstrap_python=
  # Prefer an already-activated environment before scanning PATH by version
  # name: the activated interpreter is the user's explicit context, and a
  # higher-numbered python3.NN on PATH (for example a Homebrew interpreter
  # without setuptools) would otherwise win over it.
  for candidate in \
    "${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}" \
    "${CONDA_PREFIX:+$CONDA_PREFIX/bin/python}" \
    python3.14 python3.13 python3.12 python3.11 python3; do
    [ -n "$candidate" ] || continue
    if try_bootstrap_candidate "$candidate"; then
      break
    fi
  done
  if [ -z "$bootstrap_python" ]; then
    echo "roundtable-install: CPython 3.11 through 3.14 is required; no supported interpreter was found on PATH" >&2
    if [ "$build_from_source" = yes ]; then
      echo "a source install also needs setuptools in that interpreter; use a release --wheel-dir for an offline install without a build" >&2
    fi
    echo "set ROUNDTABLE_BOOTSTRAP_PYTHON=/absolute/path/to/a/supported/python3" >&2
    exit 1
  fi
fi

if [ "$mode" = source ] && [ -d "$source_root/wheels" ]; then
  set -- --wheel-dir "$source_root/wheels" "$@"
elif [ "$mode" = source ]; then
  set -- --source-root "$source_root" "$@"
fi

PYTHONPATH="$source_root${PYTHONPATH:+:$PYTHONPATH}" \
  exec "$bootstrap_python" -m roundtable_packaging.cli \
  install "$@"
