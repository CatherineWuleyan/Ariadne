#!/usr/bin/env bash
# One command that CI and a human can share: run every suite, then smoke-test
# the offline demo and assert its run record is complete.
#
#   bash scripts/check.sh
#
# Exits non-zero on the first failure. No network, no model key, no LKM call.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv-gaia-a7/bin/python}"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi

export PYTHONPATH="$ROOT:$ROOT/agent:$ROOT/pipelines/merge:$ROOT/pipelines/single_paper:$ROOT/pipelines/clean_claims"
export ARIADNE_PYTHON="$PY"

echo "== python: $("$PY" -V 2>&1) ($PY)"

echo
# Each suite runs in its own pytest process. Several suites share test file
# basenames (test_view.py, test_step3.py, ...) and `agent/` is not a package, so
# collecting them together makes pytest import the wrong module.
for suite in ariadne/tests pipelines/merge/tests pipelines/single_paper/tests agent/tests; do
  echo "== tests: $suite"
  "$PY" -m pytest "$suite" -q
done

echo
echo "== offline demo (frozen aircraft graph)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
"$PY" -m ariadne demo --out "$TMP" >/dev/null

"$PY" - "$TMP" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
runs = [p for p in out.rglob("run.json")]
assert runs, f"demo wrote no run.json under {out}"
record = json.loads(runs[0].read_text(encoding="utf-8"))
assert record["status"] == "complete", record.get("status")
assert record["mode"] == "demo"
assert record["stages"], "demo record has no stage timings"
repro = record["reproducibility"]
assert "commit" in repro["code"], repro["code"]
assert repro["inputs"]["view_sha256"], repro["inputs"]

viewer = Path(record["outputs"]["viewer"])
chains = Path(record["outputs"]["chains"])
assert viewer.is_file(), viewer
assert chains.is_file(), chains
assert "same-paper" in chains.read_text(encoding="utf-8")
print(f"   demo run record ok: {runs[0].parent.name}")
PY

echo
echo "check: ok"
