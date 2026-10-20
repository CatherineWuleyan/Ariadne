from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import ArtifactRef, Checkpoint, JSONDict, RunRecord, utc_now


SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9_-]{1,12}$")


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value))


def read_json(path: Path) -> JSONDict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def resolve_within(root: Path, relative_path: str) -> Path:
    if not relative_path or Path(relative_path).is_absolute():
        raise ValueError("path must be non-empty and relative")
    resolved_root = root.resolve()
    candidate = (resolved_root / relative_path).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValueError(f"path escapes run directory: {relative_path}")
    return candidate


def validate_pipeline_config(config: Mapping[str, Any]) -> None:
    pipeline_id = config.get("pipeline_id")
    version = config.get("version")
    stages = config.get("stages")
    if not isinstance(pipeline_id, str) or not pipeline_id:
        raise ValueError("pipeline_id must be a non-empty string")
    if not isinstance(version, str) or not version:
        raise ValueError("pipeline version must be a non-empty string")
    if not isinstance(stages, list) or not stages:
        raise ValueError("pipeline stages must be a non-empty list")
    names: set[str] = set()
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise ValueError(f"stage {index} must be an object")
        name = stage.get("name")
        plugin = stage.get("plugin")
        options = stage.get("options", {})
        if not isinstance(name, str) or not name:
            raise ValueError(f"stage {index} name must be a non-empty string")
        if name in names:
            raise ValueError(f"duplicate stage name: {name}")
        names.add(name)
        if not isinstance(plugin, str) or ":" not in plugin:
            raise ValueError(f"stage {name} plugin must use module:object syntax")
        if not isinstance(options, dict):
            raise ValueError(f"stage {name} options must be an object")
    view_adapter = config.get("view_adapter")
    if view_adapter is not None and (
        not isinstance(view_adapter, str) or ":" not in view_adapter
    ):
        raise ValueError("view_adapter must use module:object syntax")


def load_input_manifest(path: Path | str) -> list[tuple[Path, JSONDict]]:
    manifest_path = Path(path).resolve()
    payload = read_json(manifest_path)
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("input manifest must contain an artifacts list")
    prepared: list[tuple[Path, JSONDict]] = []
    for index, item in enumerate(artifacts):
        if not isinstance(item, dict):
            raise ValueError(f"input artifact {index} must be an object")
        declared_path = item.get("path")
        path_glob = item.get("path_glob")
        kind = item.get("kind")
        declared_media_type = item.get("media_type")
        metadata = item.get("metadata", {})
        required = item.get("required", True)
        if (declared_path is None) == (path_glob is None):
            raise ValueError(f"input artifact {index} must define exactly one of path or path_glob")
        if declared_path is not None and (not isinstance(declared_path, str) or not declared_path):
            raise ValueError(f"input artifact {index} path must be a non-empty string")
        if path_glob is not None and (not isinstance(path_glob, str) or not path_glob):
            raise ValueError(f"input artifact {index} path_glob must be a non-empty string")
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"input artifact {index} kind must be a non-empty string")
        if declared_media_type is not None and (not isinstance(declared_media_type, str) or not declared_media_type):
            raise ValueError(f"input artifact {index} media_type must be a non-empty string")
        if not isinstance(metadata, dict):
            raise ValueError(f"input artifact {index} metadata must be an object")
        if not isinstance(required, bool):
            raise ValueError(f"input artifact {index} required must be a boolean")
        if path_glob is not None:
            if Path(path_glob).is_absolute():
                raise ValueError(f"input artifact {index} path_glob must be relative")
            source_paths = sorted(candidate.resolve() for candidate in manifest_path.parent.glob(path_glob) if candidate.is_file())
            if required and not source_paths:
                raise ValueError(f"input artifact {index} path_glob matched no files: {path_glob}")
        else:
            source_path = Path(declared_path)
            if not source_path.is_absolute():
                source_path = manifest_path.parent / source_path
            source_paths = [source_path.resolve()]
            if not source_paths[0].is_file():
                raise ValueError(f"input artifact source is not a file: {source_paths[0]}")
        for sequence, source_path in enumerate(source_paths, 1):
            normalized_path = str(source_path.relative_to(manifest_path.parent)) if source_path.is_relative_to(manifest_path.parent) else str(source_path)
            media_type = declared_media_type or mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
            item_metadata = dict(metadata)
            if path_glob is not None:
                item_metadata.update({"manifest_glob": path_glob, "sequence": sequence, "source_filename": source_path.name})
            prepared.append((source_path, {"path": normalized_path, "kind": kind, "media_type": media_type, "metadata": item_metadata}))
    return prepared


class RunStore:
    def __init__(self, run_dir: Path | str):
        self.run_dir = Path(run_dir).resolve()

    @classmethod
    def create(
        cls,
        runs_root: Path | str,
        config: Mapping[str, Any],
        *,
        input_manifest: Path | str | None = None,
    ) -> "RunStore":
        validate_pipeline_config(config)
        prepared_inputs = load_input_manifest(input_manifest) if input_manifest is not None else []
        root = Path(runs_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"run_{timestamp}_{uuid.uuid4().hex[:8]}"
        run_dir = root / run_id
        run_dir.mkdir(parents=False, exist_ok=False)
        for directory in ("inputs", "artifacts/blobs", "checkpoints", "views", "work"):
            (run_dir / directory).mkdir(parents=True, exist_ok=False)
        atomic_write_json(run_dir / "artifacts/index.json", {"artifacts": []})
        atomic_write_bytes(run_dir / "events.ndjson", b"")
        now = utc_now()
        stages = config["stages"]
        record = RunRecord(
            run_id=run_id,
            pipeline_id=str(config["pipeline_id"]),
            pipeline_version=str(config["version"]),
            status="created",
            current_stage=stages[0]["name"],
            config=dict(config),
            config_sha256=sha256_json(dict(config)),
            created_at=now,
            updated_at=now,
        )
        store = cls(run_dir)
        store.save_run(record)
        store.append_event("run_created", {"pipeline_id": record.pipeline_id})
        input_refs: list[ArtifactRef] = []
        for source_path, item in prepared_inputs:
            metadata = dict(item["metadata"])
            metadata["_harness"] = {
                "role": "input",
                "declared_path": item["path"],
            }
            input_refs.append(
                store.register_file(
                    source_path,
                    kind=str(item["kind"]),
                    media_type=str(item["media_type"]),
                    producer_stage="input",
                    metadata=metadata,
                )
            )
        record.input_artifacts = [ref.artifact_id for ref in input_refs]
        store.save_run(record)
        store.write_input_manifest(
            input_refs,
            source_manifest=(
                str(Path(input_manifest).resolve()) if input_manifest is not None else None
            ),
        )
        return store

    @property
    def run_path(self) -> Path:
        return self.run_dir / "run.json"

    @property
    def artifact_index_path(self) -> Path:
        return self.run_dir / "artifacts/index.json"

    @property
    def input_manifest_path(self) -> Path:
        return self.run_dir / "inputs/manifest.json"

    def load_run(self) -> RunRecord:
        return RunRecord.from_dict(read_json(self.run_path))

    def save_run(self, run: RunRecord) -> None:
        run.__post_init__()
        run.updated_at = utc_now()
        atomic_write_json(self.run_path, run.to_dict())

    def write_input_manifest(
        self,
        refs: Iterable[ArtifactRef],
        *,
        source_manifest: str | None,
        forked_from: Mapping[str, str] | None = None,
    ) -> None:
        artifacts = []
        for ref in refs:
            harness_metadata = ref.metadata.get("_harness", {})
            declared_path = (
                harness_metadata.get("declared_path")
                if isinstance(harness_metadata, dict)
                else None
            )
            artifacts.append(
                {
                    "artifact_id": ref.artifact_id,
                    "kind": ref.kind,
                    "media_type": ref.media_type,
                    "sha256": ref.sha256,
                    "declared_path": declared_path,
                }
            )
        payload: JSONDict = {
            "schema_version": "1",
            "source_manifest": source_manifest,
            "artifacts": artifacts,
        }
        if forked_from is not None:
            payload["forked_from"] = dict(forked_from)
        atomic_write_json(self.input_manifest_path, payload)
        self.append_event(
            "inputs_frozen",
            {"artifacts": [item["artifact_id"] for item in artifacts]},
        )

    def append_event(self, event: str, details: Mapping[str, Any] | None = None) -> None:
        if not event:
            raise ValueError("event must be non-empty")
        payload = canonical_json_bytes(
            {"event": event, "at": utc_now(), "details": dict(details or {})}
        )
        events_path = self.run_dir / "events.ndjson"
        with events_path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    def load_artifacts(self) -> list[ArtifactRef]:
        payload = read_json(self.artifact_index_path)
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("artifact index must contain an artifacts list")
        return [ArtifactRef.from_dict(item) for item in artifacts]

    def artifact_map(self) -> dict[str, ArtifactRef]:
        refs = self.load_artifacts()
        result = {ref.artifact_id: ref for ref in refs}
        if len(result) != len(refs):
            raise ValueError("artifact index contains duplicate artifact IDs")
        return result

    def artifact_path(self, ref: ArtifactRef) -> Path:
        return resolve_within(self.run_dir, ref.path)

    def register_file(
        self,
        source: Path | str,
        *,
        kind: str,
        media_type: str,
        producer_stage: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRef:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise ValueError(f"artifact source is not a file: {source_path}")
        content = source_path.read_bytes()
        digest = sha256_bytes(content)
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        suffix = source_path.suffix if SAFE_SUFFIX.match(source_path.suffix) else ".bin"
        relative_path = f"artifacts/blobs/{artifact_id}{suffix}"
        destination = resolve_within(self.run_dir, relative_path)
        atomic_write_bytes(destination, content)
        ref = ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            path=relative_path,
            sha256=digest,
            media_type=media_type,
            producer_stage=producer_stage,
            metadata=dict(metadata or {}),
        )
        refs = self.load_artifacts()
        refs.append(ref)
        atomic_write_json(
            self.artifact_index_path, {"artifacts": [item.to_dict() for item in refs]}
        )
        self.append_event(
            "artifact_registered",
            {"artifact_id": artifact_id, "kind": kind, "sha256": digest},
        )
        return ref

    def create_checkpoint(self, stage: str, artifact_ids: Iterable[str]) -> Checkpoint:
        ids = list(artifact_ids)
        known = self.artifact_map()
        missing = [artifact_id for artifact_id in ids if artifact_id not in known]
        if missing:
            raise ValueError(f"checkpoint has unknown artifact IDs: {', '.join(missing)}")
        checkpoint = Checkpoint(
            checkpoint_id=f"checkpoint_{uuid.uuid4().hex}",
            stage=stage,
            artifacts=ids,
            created_at=utc_now(),
        )
        atomic_write_json(
            self.run_dir / "checkpoints" / f"{checkpoint.checkpoint_id}.json",
            checkpoint.to_dict(),
        )
        self.append_event(
            "checkpoint_created",
            {"checkpoint_id": checkpoint.checkpoint_id, "stage": stage, "artifacts": ids},
        )
        return checkpoint

    def load_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        if not checkpoint_id or "/" in checkpoint_id or ".." in checkpoint_id:
            raise ValueError("invalid checkpoint ID")
        path = self.run_dir / "checkpoints" / f"{checkpoint_id}.json"
        return Checkpoint.from_dict(read_json(path))

    def list_checkpoints(self) -> list[Checkpoint]:
        checkpoints: list[Checkpoint] = []
        for path in sorted((self.run_dir / "checkpoints").glob("checkpoint_*.json")):
            checkpoints.append(Checkpoint.from_dict(read_json(path)))
        return checkpoints

    @classmethod
    def fork(
        cls,
        source_run_dir: Path | str,
        checkpoint_id: str,
        *,
        reason: str | None = None,
        additional_input_manifest: Path | str | None = None,
    ) -> "RunStore":
        source = cls(source_run_dir)
        prepared_additional_inputs = (
            load_input_manifest(additional_input_manifest)
            if additional_input_manifest is not None
            else []
        )
        parent_run = source.load_run()
        if sha256_json(parent_run.config) != parent_run.config_sha256:
            raise ValueError("parent run config does not match its frozen hash")
        parent_checkpoint = source.load_checkpoint(checkpoint_id)
        stages = parent_run.config.get("stages")
        if not isinstance(stages, list):
            raise ValueError("parent run config has no stages")
        stage_names = [stage.get("name") for stage in stages if isinstance(stage, dict)]
        if parent_checkpoint.stage not in stage_names:
            raise ValueError(
                f"checkpoint stage is not present in parent config: {parent_checkpoint.stage}"
            )
        next_stage_index = stage_names.index(parent_checkpoint.stage) + 1
        parent_artifacts = source.artifact_map()
        inherited_ids = list(
            dict.fromkeys(parent_run.input_artifacts + parent_checkpoint.artifacts)
        )
        for artifact_id in inherited_ids:
            parent_ref = parent_artifacts.get(artifact_id)
            if parent_ref is None:
                raise ValueError(f"checkpoint lineage references unknown artifact: {artifact_id}")
            if sha256_file(source.artifact_path(parent_ref)) != parent_ref.sha256:
                raise ValueError(f"parent artifact hash mismatch: {artifact_id}")

        child = cls.create(source.run_dir.parent, parent_run.config)
        child_run = child.load_run()
        copied: dict[str, ArtifactRef] = {}

        def copy_ref(parent_ref: ArtifactRef, *, producer_stage: str) -> ArtifactRef:
            existing = copied.get(parent_ref.artifact_id)
            if existing is not None:
                return existing
            metadata = dict(parent_ref.metadata)
            metadata["_fork"] = {
                "source_run_id": parent_run.run_id,
                "source_artifact_id": parent_ref.artifact_id,
                "source_sha256": parent_ref.sha256,
            }
            ref = child.register_file(
                source.artifact_path(parent_ref),
                kind=parent_ref.kind,
                media_type=parent_ref.media_type,
                producer_stage=producer_stage,
                metadata=metadata,
            )
            copied[parent_ref.artifact_id] = ref
            return ref

        child_inputs = [
            copy_ref(parent_artifacts[artifact_id], producer_stage="input")
            for artifact_id in parent_run.input_artifacts
        ]
        for source_path, item in prepared_additional_inputs:
            metadata = dict(item["metadata"])
            metadata["_harness"] = {
                "role": "input",
                "declared_path": item["path"],
            }
            child_inputs.append(
                child.register_file(
                    source_path,
                    kind=str(item["kind"]),
                    media_type=str(item["media_type"]),
                    producer_stage="input",
                    metadata=metadata,
                )
            )
        child_checkpoint_artifacts = [
            copy_ref(
                parent_artifacts[artifact_id],
                producer_stage=parent_artifacts[artifact_id].producer_stage,
            )
            for artifact_id in parent_checkpoint.artifacts
        ]
        child_checkpoint = child.create_checkpoint(
            parent_checkpoint.stage,
            [ref.artifact_id for ref in child_checkpoint_artifacts],
        )
        child_run.input_artifacts = [ref.artifact_id for ref in child_inputs]
        child_run.parent_run_id = parent_run.run_id
        child_run.parent_checkpoint_id = parent_checkpoint.checkpoint_id
        child_run.next_stage_index = next_stage_index
        child_run.last_checkpoint_id = child_checkpoint.checkpoint_id
        child_run.current_stage = (
            str(stages[next_stage_index]["name"])
            if next_stage_index < len(stages)
            else None
        )
        child_run.status = "created" if child_run.current_stage is not None else "succeeded"
        child_run.waiting = None
        child.save_run(child_run)
        child.write_input_manifest(
            child_inputs,
            source_manifest=(
                str(Path(additional_input_manifest).resolve())
                if additional_input_manifest is not None
                else None
            ),
            forked_from={
                "run_id": parent_run.run_id,
                "checkpoint_id": parent_checkpoint.checkpoint_id,
            },
        )
        child.append_event(
            "run_forked",
            {
                "parent_run_id": parent_run.run_id,
                "parent_checkpoint_id": parent_checkpoint.checkpoint_id,
                "reason": reason,
                "additional_input_artifacts": [
                    ref.artifact_id
                    for ref in child_inputs[len(parent_run.input_artifacts):]
                ],
            },
        )
        return child
