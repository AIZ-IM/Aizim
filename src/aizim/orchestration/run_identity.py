from __future__ import annotations

from aizim.domain import sha256_bytes


def contribution_id(run_id: str, worker_id: str) -> str:
    _validate(run_id, worker_id)
    return f"{run_id}:{worker_id}"


def candidate_name(run_id: str, worker_id: str) -> str:
    _validate(run_id, worker_id)
    stem = {
        "prover-a": "a_add_zero",
        "prover-b": "b_use_a",
    }.get(worker_id, "candidate")
    digest = sha256_bytes(f"{run_id}\0{worker_id}".encode())[:16]
    return f"{stem}_{digest}"


def _validate(run_id: str, worker_id: str) -> None:
    if type(run_id) is not str or not run_id or type(worker_id) is not str or not worker_id:
        raise ValueError("INVALID_RUN_IDENTITY")
