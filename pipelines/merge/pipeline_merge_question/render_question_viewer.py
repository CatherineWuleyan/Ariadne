#!/usr/bin/env python3
"""Inject a projected view model into the question viewer template.

Use this after ``pipeline_harness view --run <run>`` (which writes the generic
``views/view_model.json``) when the run used the question view adapter:

    python3 pipelines/merge/pipeline_merge_question/render_question_viewer.py \
        --view-model <run>/views/view_model.json \
        --out <run>/views/domain_viewer_question.html

The template works with either location of the answer layer:
top-level ``answer_layer`` (standalone runner) or
``metadata.answer_layer`` (pipeline view adapter).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = HERE / "viewer_question.html"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--view-model", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    args = parser.parse_args()

    view = json.loads(args.view_model.read_text(encoding="utf-8"))
    template = args.template.read_text(encoding="utf-8")
    if template.count("__VIEW_DATA__") != 1:
        raise SystemExit("viewer template must contain exactly one __VIEW_DATA__ placeholder")
    payload = json.dumps(view, ensure_ascii=False).replace("</", "<\\/")
    html = template.replace("__VIEW_DATA__", payload)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
