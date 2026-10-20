#!/usr/bin/env bash
# Ariadne bundle installer: create a local venv and install dependencies.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
PYTHON="${PYTHON:-python3}"
NO_VENV=0
NO_DEPS=0

usage() {
  cat <<'USAGE'
Usage: ./install.sh [options]

Options:
  --venv PATH     virtualenv directory (default: ./.venv)
  --no-venv       install into the selected Python instead of a new venv
  --python PATH   Python interpreter (default: python3)
  --no-deps       create/skip the venv without installing packages
  -h, --help      show this help

After install:
  ./run.sh config init
  ./run.sh doctor
  ./run.sh demo
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv) VENV="$2"; shift 2 ;;
    --no-venv) NO_VENV=1; shift ;;
    --python) PYTHON="$2"; shift 2 ;;
    --no-deps) NO_DEPS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Ariadne: Python not found: $PYTHON" >&2
  exit 2
fi

if [[ "$NO_VENV" -eq 0 ]]; then
  if [[ ! -d "$VENV" ]]; then
    echo "[install] creating venv: $VENV"
    "$PYTHON" -m venv "$VENV"
  fi
  PY="$VENV/bin/python"
  if [[ ! -x "$PY" ]]; then
    echo "Ariadne: venv Python not found: $PY" >&2
    exit 2
  fi
else
  PY="$(command -v "$PYTHON")"
fi

"$PY" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(
        "Ariadne requires Python 3.11+; found %s.%s" % sys.version_info[:2]
    )
print("[install] python:", sys.executable)
PY

if [[ "$NO_DEPS" -eq 0 ]]; then
  echo "[install] upgrading pip"
  "$PY" -m pip install --upgrade pip
  echo "[install] installing Ariadne dependencies"
  "$PY" -m pip install -r "$ROOT/requirements.txt"
  echo "[install] installing pipeline harness"
  "$PY" -m pip install -e "$ROOT/agent"
fi

echo
echo "Ariadne installed."
echo "  Python: $PY"
echo
echo "Next:"
echo "  ./run.sh config init"
echo "  ./run.sh doctor"
echo "  ./run.sh demo"
