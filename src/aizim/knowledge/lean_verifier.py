from __future__ import annotations

import os
import re
from pathlib import PurePosixPath

from aizim.domain import EpochPair, sha256_bytes
from aizim.lean import DocumentBroker, SharedLeanRuntime
from aizim.lean.document_io import (
    DocumentIoError,
    create_relative,
    mode_relative,
    open_root,
    read_relative,
    replace_relative,
    unlink_relative,
)
from aizim.lean.models import DiagnosticsResult
from aizim.lean.project import project_base_epoch
from aizim.lean.promotion_runtime import PromotionCheck

from .errors import PromotionError
from .promotion_types import PromotionEvidence, PromotionMaterialization


class RuntimePromotionVerifier:
    def __init__(self, runtime: SharedLeanRuntime, broker: DocumentBroker, run_id: str) -> None:
        if type(runtime) is not SharedLeanRuntime or type(broker) is not DocumentBroker:
            raise PromotionError("INVALID_RUNTIME_VERIFIER")
        if type(run_id) is not str or not run_id:
            raise PromotionError("INVALID_RUNTIME_VERIFIER")
        self._runtime, self._broker, self._run_id = runtime, broker, run_id

    async def verify(self, source: bytes, theorem_name: str) -> PromotionEvidence:
        module_source = _render(source, theorem_name)
        project_root = await self._broker._trusted_promotion_project(self._run_id)
        suffix = sha256_bytes(module_source)[:16]
        module = PurePosixPath("AizimSmoke/Research") / f"Promotion_{suffix}.lean"
        probe = PurePosixPath("AizimSmoke/Research") / f"PromotionCheck_{suffix}.lean"
        descriptor, created = open_root(project_root), []
        manifest = PurePosixPath("AizimSmoke.lean")
        original = read_relative(descriptor, manifest)
        try:
            _stage(descriptor, module, module_source, created)
            import_name = module.with_suffix("").as_posix().replace("/", ".")
            _stage(
                descriptor,
                probe,
                f"import {import_name}\n#check {theorem_name}\n".encode(),
                created,
            )
            replace_relative(descriptor, manifest, _with_import(original, module))
            check = await self._runtime._promotion_check(
                project_root, project_root / module, theorem_name, project_root / probe
            )
        finally:
            try:
                replace_relative(descriptor, manifest, original)
                for path in created:
                    unlink_relative(descriptor, path)
            finally:
                os.close(descriptor)
        errors = _errors(check)
        return PromotionEvidence(
            errors,
            check.verification.axioms,
            _complete_type(check.type_diagnostics, theorem_name, bool(errors)),
            _imports(module_source),
            check.verification.axioms,
            module_source,
        )

    async def materialize(
        self, source: bytes, epoch_pair: EpochPair, publication_sequence: int
    ) -> PromotionMaterialization:
        if type(source) is not bytes or type(epoch_pair) is not EpochPair:
            raise PromotionError("INVALID_MATERIALIZATION")
        if type(publication_sequence) is not int or publication_sequence < 1:
            raise PromotionError("INVALID_MATERIALIZATION")
        project_root = await self._broker._trusted_promotion_project(self._run_id)
        if project_base_epoch(project_root) != epoch_pair.base_epoch:
            raise PromotionError("EPOCH_PROJECT_MISMATCH")
        module = _publication_module(epoch_pair, publication_sequence, source)
        descriptor = open_root(project_root)
        try:
            try:
                existing = read_relative(descriptor, module)
            except DocumentIoError:
                create_relative(descriptor, module, source, mode=0o444)
            else:
                if existing != source or mode_relative(descriptor, module) != 0o444:
                    raise PromotionError("PUBLICATION_MODULE_COLLISION")
        finally:
            os.close(descriptor)
        name = module.with_suffix("").as_posix().replace("/", ".")
        return PromotionMaterialization(
            name, sha256_bytes(source), project_base_epoch(project_root, {name: source})
        )

    async def activate(self, materialization: PromotionMaterialization) -> None:
        project_root = await self._broker._trusted_promotion_project(self._run_id)
        module = PurePosixPath(*materialization.module.split(".")).with_suffix(".lean")
        descriptor = open_root(project_root)
        try:
            source = read_relative(descriptor, module)
            if (
                sha256_bytes(source) != materialization.content_hash
                or mode_relative(descriptor, module) != 0o444
            ):
                raise PromotionError("PUBLICATION_MODULE_COLLISION")
            manifest = PurePosixPath("AizimSmoke.lean")
            original = read_relative(descriptor, manifest)
            updated = _with_import(original, module)
            if updated != original:
                replace_relative(descriptor, manifest, updated)
        finally:
            os.close(descriptor)


def _stage(
    descriptor: int, path: PurePosixPath, source: bytes, created: list[PurePosixPath]
) -> None:
    try:
        existing = read_relative(descriptor, path)
    except DocumentIoError:
        create_relative(descriptor, path, source)
        created.append(path)
    else:
        if existing != source:
            raise PromotionError("STAGING_MODULE_COLLISION")


def _render(source: bytes, theorem_name: str) -> bytes:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        raise PromotionError("INVALID_PROMOTION_SOURCE") from None
    prefix, leaf = "AizimSmoke.Research.", theorem_name.removeprefix("AizimSmoke.Research.")
    if theorem_name == leaf or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", leaf) is None:
        raise PromotionError("INVALID_PROMOTION_NAME")
    imports = [line for line in text.splitlines(keepends=True) if line.startswith("import ")]
    body = "".join(
        line for line in text.splitlines(keepends=True) if not line.startswith("import ")
    )
    if re.search(r"(?m)^\s*(namespace|end)\b", body):
        raise PromotionError("INVALID_PROMOTION_SOURCE")
    matches = list(re.finditer(r"\btheorem\s+([A-Za-z_][A-Za-z0-9_]*)", body))
    if len(matches) != 1:
        raise PromotionError("INVALID_PROMOTION_SOURCE")
    match = matches[0]
    rewritten = body[: match.start(1)] + leaf + body[match.end(1) :]
    return (
        "".join(imports) + f"\nnamespace {prefix[:-1]}\n\n" + rewritten + f"\nend {prefix[:-1]}\n"
    ).encode()


def _errors(check: PromotionCheck) -> tuple[str, ...]:
    diagnostics = check.diagnostics
    errors = tuple(item.message for item in diagnostics.items if item.severity == "error")
    if diagnostics.timed_out or diagnostics.partial or not diagnostics.success:
        errors += ("INCOMPLETE_DIAGNOSTICS",)
    if not check.build.success:
        errors += check.build.errors or ("LEAN_BUILD_FAILED",)
    type_diagnostics = check.type_diagnostics
    errors += tuple(item.message for item in type_diagnostics.items if item.severity == "error")
    if type_diagnostics.timed_out or type_diagnostics.partial or not type_diagnostics.success:
        errors += ("INCOMPLETE_TYPE_DIAGNOSTICS",)
    return errors


def _complete_type(diagnostics: DiagnosticsResult, theorem_name: str, failed: bool) -> str:
    prefix = f"{theorem_name} : "
    for item in diagnostics.items:
        if item.severity == "info" and item.message.startswith(prefix):
            return item.message.removeprefix(prefix)
    if failed:
        return "Lean.Error"
    raise PromotionError("TRUSTED_TYPE_UNAVAILABLE")


def _imports(source: bytes) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                line.removeprefix(b"import ").strip().decode()
                for line in source.splitlines()
                if line.startswith(b"import ")
            }
        )
    )


def _publication_module(epoch_pair: EpochPair, sequence: int, source: bytes) -> PurePosixPath:
    name = f"K{epoch_pair.knowledge_epoch + 1:08d}_S{sequence:08d}_{sha256_bytes(source)}"
    return PurePosixPath("AizimSmoke/Research") / f"{name}.lean"


def _with_import(manifest: bytes, module: PurePosixPath) -> bytes:
    line = f"import {module.with_suffix('').as_posix().replace('/', '.')}\n".encode()
    return (
        manifest
        if line in manifest.splitlines(keepends=True)
        else manifest + (b"" if manifest.endswith(b"\n") else b"\n") + line
    )
