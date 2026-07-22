from __future__ import annotations

from aizim.knowledge.lean_verifier import _complete_type, _promotion_evidence
from aizim.knowledge.promotion_types import accepted
from aizim.lean.models import DiagnosticsResult
from aizim.lean.promotion_runtime import PromotionCheck
from aizim.lean.verification import BuildResult, VerificationResult


def test_trusted_hover_type_preserves_parameterized_theorem_signature() -> None:
    name = "AizimSmoke.Research.a_add_zero"

    assert _complete_type(f"{name} (n : Nat) : n + 0 = n", name, False) == "(n : Nat) : n + 0 = n"


def test_trusted_source_scan_warnings_fail_promotion_evidence() -> None:
    name = "AizimSmoke.Research.warning_candidate"
    check = PromotionCheck(
        DiagnosticsResult(False, None, True, False, (), (), "a" * 64),
        BuildResult(True, "", (), "b" * 64),
        VerificationResult((), ("declaration uses a disallowed source form",), "c" * 64),
        f"{name} : True",
    )

    evidence = _promotion_evidence(
        check, b"import Std\ntheorem warning_candidate : True := by trivial\n", name
    )

    assert evidence.source_scan_warnings == ("declaration uses a disallowed source form",)
    assert not accepted(evidence)
