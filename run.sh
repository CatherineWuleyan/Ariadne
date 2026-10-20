#!/usr/bin/env bash
# Ariadne launcher.  Set ARIADNE_PYTHON to override the interpreter, or
# configure it persistently with `./run.sh config set runtime.python`.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$HERE"

if [[ -n "${ARIADNE_PYTHON:-}" ]]; then
  PY="$(command -v "$ARIADNE_PYTHON" || printf '%s' "$ARIADNE_PYTHON")"
elif [[ -x "$REPO/.venv-gaia-a7/bin/python" ]]; then
  PY="$REPO/.venv-gaia-a7/bin/python"
elif [[ -x "$REPO/.venv/bin/python" ]]; then
  PY="$REPO/.venv/bin/python"
else
  PY="$(command -v python3 || true)"
fi

if [[ -z "$PY" || ! -x "$PY" ]]; then
  echo "Ariadne: no Python interpreter found." >&2
  echo "Create a venv and install the dependencies, or set ARIADNE_PYTHON." >&2
  exit 2
fi

export PYTHONUTF8=1
export PYTHONPATH="$REPO:$REPO/agent:$REPO/pipelines/single_paper:$REPO/pipelines/merge${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m ariadne "$@"
