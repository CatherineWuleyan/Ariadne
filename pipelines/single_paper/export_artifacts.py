#!/usr/bin/env python3
"""Export the latest single-paper artifacts into the package layout used by merge."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# artifact kind -> exported file name
EXPORTS = {
    "gaia.ir": "gaia.ir.json",
    "formalization": "formalization.json",
    "knowledge.index": "knowledge.index.json",
}


def export_run(run_dir: Path, paper_id: str, out_root: Path) -> dict[str, Path]:
    index_path = run_dir / "artifacts" / "index.json"
    if not index_path.is_file():
        raise SystemExit(f"no artifact index at {index_path}")

    entries = json.loads(index_path.read_text(encoding="utf-8"))["artifacts"]
    latest: dict[str, dict] = {}
    for entry in entries:  # index order == production order
        if entry.get("kind") in EXPORTS:
            latest[entry["kind"]] = entry

    missing = sorted(set(EXPORTS) - set(latest))
    if missing:
        raise SystemExit(f"run is missing artifact kinds: {', '.join(missing)}")

    target_dir = out_root / "graph_single" / paper_id
    target_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for kind, name in EXPORTS.items():
        entry = latest[kind]
        source = run_dir / entry["path"]
        if not source.is_file():
            raise SystemExit(f"blob missing for {kind}: {source}")
        destination = target_dir / name
        shutil.copyfile(source, destination)
        written[kind] = destination
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=Path, help="harness run directory")
    ap.add_argument("paper_id", help="paper id this run was produced for")
    ap.add_argument("--out", type=Path, required=True, help="export root (gets graph_single/<paper_id>/)")
    args = ap.parse_args()

    written = export_run(args.run_dir, args.paper_id, args.out)
    for kind, path in written.items():
        print(f"{kind:<18} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
