from __future__ import annotations

from aizim.knowledge.lean_verifier import _complete_type


def test_trusted_hover_type_preserves_parameterized_theorem_signature() -> None:
    name = "AizimSmoke.Research.a_add_zero"

    assert _complete_type(f"{name} (n : Nat) : n + 0 = n", name, False) == "(n : Nat) : n + 0 = n"
