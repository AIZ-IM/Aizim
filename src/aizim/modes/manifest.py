from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from aizim.config.model import RunManifest
from aizim.domain import canonical_json, sha256_bytes
from aizim.domain.serialization import JsonValue
from aizim.lean.document_io import (
    DocumentIoError,
    create_relative,
    mode_relative,
    open_root,
    read_relative,
)
from aizim.state import AppendEventCommand, StateService

from .evaluation import EvaluationLabels

SMOKE_TEST_LIMITATION = (
    "This result is an engineering smoke test and is not evidence of open-problem, novelty, "
    "or general autonomous proving capability."
)
RUNTIME_ACCEPTANCE_SCOPE = (
    "Runtime acceptance 16/16 covers replayable real-run predicates only and does not "
    "independently prove CI-only or test-only rows of the overall handoff matrix."
)
_ARTIFACT_NAMES = frozenset(
    {
        "run-manifest.json",
        "formal-trace.jsonl",
        "formal-trace.sha256",
        "alignment-review.json",
        "acceptance-report.json",
    }
)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


@dataclass(frozen=True, slots=True)
class NamedArtifact:
    artifact_name: str
    content_hash: str
    relative_path: PurePosixPath
    media_type: str
    byte_length: int


class ManifestArtifactError(RuntimeError):
    pass


def canonical_manifest(manifest: RunManifest) -> bytes:
    if type(manifest) is not RunManifest:
        raise ValueError("INVALID_RUN_MANIFEST")
    return canonical_json(manifest)


def manifest_hash(manifest: RunManifest) -> str:
    return sha256_bytes(canonical_manifest(manifest))


def manifest_document(manifest: RunManifest) -> dict[str, JsonValue]:
    value: JsonValue = json.loads(canonical_manifest(manifest))
    if type(value) is not dict:
        raise ValueError("INVALID_MANIFEST_DOCUMENT")
    return value


def write_named_artifact(
    project_root: Path, run_id: str, artifact_name: str, body: bytes, media_type: str
) -> NamedArtifact:
    if (
        not isinstance(project_root, Path)
        or _RUN_ID.fullmatch(run_id) is None
        or artifact_name not in _ARTIFACT_NAMES
        or type(body) is not bytes
        or type(media_type) is not str
        or not media_type
    ):
        raise ManifestArtifactError("INVALID_NAMED_ARTIFACT")
    relative = PurePosixPath(".aizim") / "artifacts" / run_id / artifact_name
    root = open_root(project_root)
    try:
        try:
            create_relative(root, relative, body)
        except DocumentIoError:
            existing = read_relative(root, relative)
            if existing != body:
                raise ManifestArtifactError("ARTIFACT_ALREADY_EXISTS") from None
        stored = read_relative(root, relative)
        if stored != body or mode_relative(root, relative) != 0o600:
            raise ManifestArtifactError("ARTIFACT_INTEGRITY_MISMATCH")
    except DocumentIoError as error:
        raise ManifestArtifactError("ARTIFACT_PATH_DENIED") from error
    finally:
        os.close(root)
    return NamedArtifact(artifact_name, sha256_bytes(body), relative, media_type, len(body))


def register_artifact(state: StateService, run_id: str, artifact: NamedArtifact) -> None:
    if type(state) is not StateService or type(artifact) is not NamedArtifact:
        raise ManifestArtifactError("INVALID_ARTIFACT_REGISTRATION")
    state.append_event(
        AppendEventCommand(
            "ArtifactRegistered",
            "evaluation_artifacts",
            run_id,
            None,
            {
                "artifact_name": artifact.artifact_name,
                "content_hash": artifact.content_hash,
                "relative_path": artifact.relative_path.as_posix(),
                "media_type": artifact.media_type,
                "byte_length": artifact.byte_length,
            },
        )
    )


def alignment_review_bytes(labels: EvaluationLabels) -> bytes:
    return canonical_json(
        {
            "review_kind": labels.alignment_kind.value,
            "reviewer": labels.alignment_reviewer,
            "verdict": labels.alignment_verdict,
        }
    )


def acceptance_report_bytes(
    labels: EvaluationLabels, verified_declarations: int, manifest_digest: str, trace_digest: str
) -> bytes:
    kernel_verdict = "pass" if verified_declarations == 2 else "failed"
    return canonical_json(
        {
            "alignment_verdict": labels.alignment_verdict or "not_reviewed",
            "engineering_smoke_statement": SMOKE_TEST_LIMITATION,
            "evaluation_verdict": labels.terminal_status,
            "kernel_verdict": kernel_verdict,
            "manifest_hash": manifest_digest,
            "participation_label": labels.formal_label.value,
            "runtime_acceptance_scope": RUNTIME_ACCEPTANCE_SCOPE,
            "trace_hash": trace_digest,
        }
    )
