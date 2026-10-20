"""View adapter: compose the compiled claim graph with the question answer layer.

The base projection is exactly :class:`pipeline_merge_question.view.MergeViewAdapter`
(the copied merge adapter).  This adapter only reads the immutable
``question.answer_layer`` artifact produced by Step 6 and adds:

* root nodes (``kind=answer_root``, ``layer=answer``),
* answer edges (``layer=answer``, ``semantic_type=support|bounds|oppose``),
* the answer layer under ``metadata.answer_layer`` for the viewer.

No LLM call happens here: re-rendering a run is deterministic and free.
"""
from __future__ import annotations

from typing import Any, Mapping

from pipeline_harness.models import ArtifactRef, JSONDict
from pipeline_harness.store import RunStore
from pipeline_harness.view.model import ViewDocument

from .question_layer import inject_answer_layer
from .view import MergeViewAdapter, _read_json


class QuestionViewAdapter:
    version = "1"

    def project(
        self,
        store: RunStore,
        artifacts: list[ArtifactRef],
        options: Mapping[str, Any] | None = None,
    ) -> ViewDocument:
        base = MergeViewAdapter().project(store, artifacts, options)
        layer_ref = next(
            (ref for ref in artifacts if ref.kind == "question.answer_layer"),
            None,
        )
        if layer_ref is None:
            raise ValueError(
                "question view needs a question.answer_layer artifact; "
                "run step6_question_layer before projecting the view"
            )
        answer_layer = _read_json(store, layer_ref)
        data = base.to_dict()
        data = inject_answer_layer(data, answer_layer)
        # ViewDocument only keeps known keys, so the full layer travels in
        # metadata; the standalone viewer accepts either location.
        data["metadata"]["answer_layer"] = answer_layer
        data["metadata"]["answer_layer_artifact"] = layer_ref.artifact_id
        data["source_artifacts"] = list(data.get("source_artifacts") or []) + [layer_ref.artifact_id]
        return ViewDocument.from_dict(data)


__all__ = ["QuestionViewAdapter"]
