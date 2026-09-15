from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from aizim.agents.codex_events import TransportEventHasher
from aizim.cli.init_command import run_init
from aizim.domain.serialization import JsonValue
from aizim.lean.document_io import DocumentIoError
from aizim.lean.project import materialize_smoke_project, project_base_epoch
from aizim.lean.source_layout import lakefile
from aizim.research.dashboard import document
from aizim.research.engine import AttemptResult, ResearchEngine
from aizim.research.records import (
    ResearchError,
    define_task,
    get,
    operator_change,
    records,
    require_task,
    save,
)
from aizim.research.report import estimate
from aizim.research.search import lean_search, memory_search
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

FIXTURE = Path(__file__).parents[1] / "fixtures/research_lean"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = Path(shutil.copytree(FIXTURE, tmp_path / "project"))
    assert run_init(root) == 0
    return root


def task(identity: str = "addition", dependencies: list[str] | None = None) -> dict[str, JsonValue]:
    return {
        "task_id": identity,
        "title": "Addition identity",
        "statement": "(n : Nat) : n + 0 = n",
        "author": "Ada",
        "imports": ["Std"],
        "depends_on": list(dependencies or []),
        "source_refs": ["local:research-note"],
        "max_rounds": 3,
        "max_failures": 3,
        "timeout_seconds": 5,
    }


def accepted(state: StateService, attempt) -> AttemptResult:
    cid, did = "contribution-" + attempt["id"], "declaration-" + attempt["id"]
    state.append_event(
        AppendEventCommand(
            "ContributionSubmitted",
            "fixture",
            attempt["run_id"],
            None,
            {"contribution_id": cid, "worker_id": attempt["worker_id"]},
        )
    )
    state.append_event(
        AppendEventCommand(
            "DeclarationPublished",
            "fixture",
            attempt["run_id"],
            None,
            {"declaration_id": did, "contribution_id": cid},
        )
    )
    return AttemptResult(
        "Verified fixture evidence.",
        (did,),
        {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10},
    )


async def test_repairs_from_memory_respects_dependencies_and_replays(project: Path) -> None:
    seen = []
    with StateService(StateServiceConfig(project, "research-test")) as state:
        define_task(state, task())
        define_task(state, task("consequence", ["addition"]))

        async def execute(target, attempt, context):
            seen.append((target["task_id"], attempt["round"]))
            if target["task_id"] == "addition" and attempt["round"] == 1:
                return AttemptResult(
                    "Try Nat.add_zero; the previous simplification was insufficient."
                )
            if target["task_id"] == "addition":
                assert "Nat.add_zero" in context
            else:
                assert require_task(state, "addition")["status"] == "verified"
            return accepted(state, attempt)

        await ResearchEngine(state, execute, "research-test", "test-model", workers=2).run()
        assert seen == [("addition", 1), ("addition", 2), ("consequence", 1)]
        assert state.replay_verify().matched
        digest = state.logical_digest()
    with StateService(StateServiceConfig(project, "research-restart")) as state:
        await ResearchEngine(state, execute, "research-restart", "test-model").run()
        assert state.logical_digest() == digest
        assert len(seen) == 3


async def test_independent_tasks_overlap_but_worker_limit_is_respected(project: Path) -> None:
    active = maximum = 0
    with StateService(StateServiceConfig(project, "parallel-test")) as state:
        for identity in ("first", "second", "third"):
            define_task(state, task(identity))

        async def execute(target, attempt, context):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            result = accepted(state, attempt)
            active -= 1
            return result

        await ResearchEngine(state, execute, "parallel-test", "test-model", workers=2).run()
        assert maximum == 2


async def test_inbox_arriving_during_round_is_consumed_at_next_boundary(project: Path) -> None:
    with StateService(StateServiceConfig(project, "inbox-test")) as state:
        define_task(state, task())

        async def execute(target, attempt, context):
            if attempt["round"] == 1:
                assert "Do not change Nat" not in context
                operator_change(
                    state,
                    {
                        "action": "inbox.add",
                        "body": {
                            "id": "guidance",
                            "task_id": "addition",
                            "author": "Ada",
                            "kind": "constraint",
                            "body": "Do not change Nat to Int.",
                        },
                    },
                )
                entry = get(state, "inbox", "guidance")
                assert entry is not None and entry["consumed_by"] == []
                return AttemptResult("Need another attempt.")
            assert "Do not change Nat" in context
            entry = get(state, "inbox", "guidance")
            assert entry is not None and entry["consumed_by"] == [attempt["id"]]
            return accepted(state, attempt)

        await ResearchEngine(state, execute, "inbox-test", "test-model").run()


async def test_failure_budget_blocks_and_review_cannot_verify(project: Path) -> None:
    with StateService(StateServiceConfig(project, "budget-test")) as state:
        define_task(state, {**task(), "max_failures": 1})

        async def execute(target, attempt, context):
            return AttemptResult("Could not prove the claim.")

        await ResearchEngine(state, execute, "budget-test", "test-model").run()
        assert require_task(state, "addition")["status"] == "blocked"
        operator_change(
            state,
            {
                "action": "review.add",
                "body": {
                    "id": "review-one",
                    "task_id": "addition",
                    "author": "Ada",
                    "kind": "research_value",
                    "verdict": "accept",
                    "body": "This question is worth pursuing.",
                },
            },
        )
        assert require_task(state, "addition")["status"] == "blocked"
        assert state.projections("verified_declarations") == ()
        entry = get(state, "review", "review-one")
        assert entry is not None and entry["identity_assurance"] == "operator_declared"


async def test_recovery_settles_an_already_published_attempt_without_reexecuting(
    project: Path,
) -> None:
    with StateService(StateServiceConfig(project, "recovery-test")) as state:
        define_task(state, task())

        async def never(*args):
            raise AssertionError("published work must not execute again")

        engine = ResearchEngine(state, never, "previous-run", "test-model")
        attempt = engine._begin(require_task(state, "addition"))
        accepted(state, attempt)
        await ResearchEngine(state, never, "next-run", "test-model").run()
        assert require_task(state, "addition")["status"] == "verified"
        assert len(records(state, "attempt")) == 1


async def test_executor_cannot_claim_verification_without_publication_evidence(
    project: Path,
) -> None:
    with StateService(StateServiceConfig(project, "untrusted-test")) as state:
        define_task(state, task())

        async def execute(*args):
            return AttemptResult("Trust me.", ("invented-declaration",))

        with pytest.raises(ResearchError, match="UNTRUSTED_VERIFICATION_RESULT"):
            await ResearchEngine(state, execute, "untrusted-test", "test-model").run()
        assert require_task(state, "addition")["status"] != "verified"


def test_targets_are_immutable_and_operator_cannot_write_execution_state(project: Path) -> None:
    with StateService(StateServiceConfig(project, "target-test")) as state:
        define_task(state, task())
        original = require_task(state, "addition")
        with pytest.raises(ResearchError):
            save(state, "task", "addition", {**original, "statement": "True"}, actor="worker")
        with pytest.raises(ResearchError, match="INVALID_TASK_FIELDS"):
            define_task(state, {**task("forged"), "status": "verified"})
        with pytest.raises(ResearchError, match="DEPENDENCY_NOT_FOUND"):
            define_task(state, task("missing", ["nonexistent"]))


def test_memory_is_scoped_and_never_promotes_a_claim(project: Path) -> None:
    with StateService(StateServiceConfig(project, "memory-test")) as state:
        define_task(state, task())
        define_task(state, task("other"))
        for identity in ("addition", "other"):
            operator_change(
                state,
                {
                    "action": "memory.add",
                    "body": {
                        "id": "memory-" + identity,
                        "task_id": identity,
                        "author": "Ada",
                        "kind": "dead_end",
                        "body": "Induction is unnecessary for this addition identity.",
                        "source_refs": [],
                    },
                },
            )
        hits = memory_search(state, "induction", task_id="addition")
        assert [entry["task_id"] for entry in hits] == ["addition"]
        assert all(entry["verified"] is False for entry in hits)


def test_ordinary_project_snapshot_copies_all_sources_and_freezes_hash(project: Path) -> None:
    before = project_base_epoch(project)
    run = materialize_smoke_project(project, "research-test", project)
    assert (run / "ResearchLab.lean").read_bytes() == (project / "ResearchLab.lean").read_bytes()
    assert project_base_epoch(run) == before
    (project / "ResearchLab.lean").write_text("import Std\n-- changed\n")
    with pytest.raises(DocumentIoError, match="RUN_PROJECT_MISMATCH"):
        materialize_smoke_project(project, "research-test", project)


def test_lean_source_search_finds_names_types_and_descriptions(project: Path) -> None:
    assert lean_search(project, "addition_identity", mode="name")[0]["module"] == "ResearchLab"
    assert lean_search(project, "natural number unchanged")[0]["name"] == "addition_identity"
    assert lean_search(project, "n + 0 = n", mode="type")
    (project / "ResearchLab.lean").write_text("import Std\ntheorem changed : True := trivial\n")
    assert not lean_search(project, "addition_identity", mode="name")


def test_lean_lakefile_is_supported(project: Path) -> None:
    (project / "lakefile.toml").unlink()
    (project / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage research_lab\nlean_lib ResearchLab\n"
    )
    assert lakefile(project).name == "lakefile.lean"
    assert run_init(project) == 0


def test_usage_is_measured_and_unknown_is_not_zero() -> None:
    stream = TransportEventHasher()
    assert stream.usage is None
    stream.add(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 200,
                    "output_tokens": 100,
                },
            }
        ).encode()
    )
    usage = stream.usage
    assert usage is not None
    assert (
        estimate(
            usage,
            {
                "input_per_million": "10",
                "cached_input_per_million": "1",
                "output_per_million": "50",
            },
        )
        == "0.013200"
    )
    stream.add(b'{"type":"turn.completed","usage":{"input_tokens":10}}')
    assert stream.usage is None


def test_static_dashboard_does_not_execute_research_text() -> None:
    dangerous = "</script><script>alert('research')</script>"
    page = document({"project": dangerous})
    assert dangerous not in page
    assert "\\u003c/script\\u003e" in page
    assert "textContent" in page
