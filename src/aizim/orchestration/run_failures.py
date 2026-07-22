from __future__ import annotations

from enum import IntEnum

from aizim.agents.codex_backend import CodexBackendError
from aizim.agents.macos_sandbox import SandboxHostError
from aizim.config.model import ConfigError
from aizim.knowledge import ContributionValidationError
from aizim.lean.document_io import DocumentIoError
from aizim.lean.models import DocumentBrokerError
from aizim.lean.path_policy import LeanPathError
from aizim.runtime.layout import LayoutError
from aizim.state.service import StateServiceLifecycleError

from .codex_worker import CodexWorkerError
from .promotion_consumer import PromotionConsumerError
from .resources import ResourcePolicyError
from .worker import WorkerExecutionError
from .worker_gateway import WorkerGatewayError


class RunFailureCategory(IntEnum):
    CONFIGURATION = 2
    READINESS = 3
    AUTHORIZATION = 4
    LEAN_VERIFICATION = 5
    RUNTIME = 6


class RunError(RuntimeError):
    def __init__(
        self,
        reason: str,
        category: RunFailureCategory = RunFailureCategory.CONFIGURATION,
    ) -> None:
        self.category = category
        super().__init__(reason)


_AUTHORIZATION_PROMOTION_FAILURES = frozenset(
    {
        "CANDIDATE_NOT_IN_SOURCE",
        "CONTENT_HASH_MISMATCH",
        "DOCUMENT_NOT_FOUND",
        "EPOCH_MISMATCH",
        "EPOCH_PROJECT_MISMATCH",
        "EXTRA_TOP_LEVEL_DECLARATION",
        "FORBIDDEN_LEAN_COMMAND",
        "FORBIDDEN_LEAN_TOKEN",
        "IMPORT_NOT_ALLOWED",
        "INVALID_BYTE_EDIT",
        "INVALID_BYTE_EDITS",
        "INVALID_CANDIDATE",
        "INVALID_CONTENT_HASH",
        "INVALID_CONTRIBUTION",
        "INVALID_FILE_VERSION",
        "INVALID_IMPORT_POLICY",
        "INVALID_PATCH_EDITS",
        "INVALID_PAYLOAD_HASH",
        "INVALID_PROMOTION_NAME",
        "INVALID_PROMOTION_SOURCE",
        "INVALID_UTF8",
        "PAYLOAD_HASH_MISMATCH",
        "PUBLICATION_MODULE_COLLISION",
        "STAGING_MODULE_COLLISION",
    }
)
_VERIFICATION_PROMOTION_FAILURES = frozenset(
    {"ACTIVATION_BUILD_FAILED", "LEAN_VERIFICATION_FAILED", "TRUSTED_TYPE_UNAVAILABLE"}
)


def run_exit_code(error: BaseException) -> RunFailureCategory:
    if isinstance(error, RunError):
        return error.category
    if isinstance(error, (ConfigError, LayoutError)):
        return RunFailureCategory.CONFIGURATION
    if isinstance(
        error,
        (
            ResourcePolicyError,
            CodexWorkerError,
            CodexBackendError,
            SandboxHostError,
            StateServiceLifecycleError,
        ),
    ):
        return RunFailureCategory.READINESS
    if isinstance(
        error,
        (
            ContributionValidationError,
            DocumentBrokerError,
            DocumentIoError,
            LeanPathError,
            WorkerGatewayError,
        ),
    ):
        return RunFailureCategory.AUTHORIZATION
    if isinstance(error, PromotionConsumerError):
        if error.reason_code in _AUTHORIZATION_PROMOTION_FAILURES:
            return RunFailureCategory.AUTHORIZATION
        if error.reason_code in _VERIFICATION_PROMOTION_FAILURES:
            return RunFailureCategory.LEAN_VERIFICATION
        return RunFailureCategory.RUNTIME
    if isinstance(error, WorkerExecutionError):
        return RunFailureCategory.RUNTIME
    return RunFailureCategory.RUNTIME
