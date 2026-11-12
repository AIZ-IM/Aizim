from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from aizim.agents import AgentRequest, AgentResult, BackendIdentity
from aizim.cli.init_command import run_init
from aizim.domain.serialization import JsonValue
from aizim.knowledge.artifacts import ArtifactStore
from aizim.lean.document_io import DocumentIoError
from aizim.lean.project import materialize_smoke_project, project_base_epoch
from aizim.lean.source_layout import lakefile, module_path
from aizim.orchestration.run_identity import candidate_name
from aizim.orchestration.worker import WorkerExecutionError
from aizim.orchestration.worker_authority import WorkerBackend, WorkerDirective
from aizim.research.dashboard import document
from aizim.research.engine import (
    AttemptResult,
    LeanExecutor,
    ResearchEngine,
)
from aizim.research.records import (
    ResearchError,
    define_task,
    frontier,
    get,
    integer,
    operator_change,
    records,
    require_task,
    save,
    text,
    texts,
)
from aizim.research.report import estimate
from aizim.research.search import lean_search, memory_search
from aizim.state import AppendEventCommand, StateService, StateServiceConfig
from aizim.state.event_payload import thaw_payload

FIXTURE = Path(__file__).parents[1] / "fixtures/research_lean"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = Path(shutil.copytree(FIXTURE, tmp_path / "project"))
    assert run_init(root) == 0
    return root


@pytest.fixture
def private_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    temporary = tmp_path / "private-tmp"
    temporary.mkdir(mode=0o700)
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    return temporary


class ScriptedBackend:
    """Stand in for the omp worker and record what the agent was told."""

    identity = BackendIdentity("fake", "test", None)

    def __init__(
        self,
        summary: str = "Tried simp; the goal n + 0 = n remained.",
        usage: dict[str, int] | None = None,
    ) -> None:
        self.summary = summary
        self.usage = usage or {"input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 5}
        self.prompts: list[str] = []

    async def run(self, request: AgentRequest) -> AgentResult:
        self.prompts.append(request.prompt)
        return AgentResult(
            request.worker_id, "abstained", self.summary, "a" * 64, "b" * 64, 0,
            usage=self.usage, model="magpie/test",
        )


class DirectHost:
    """Run the attempt backend in place of WorkerExecutionHost, then raise its stop reason."""

    def __init__(
        self,
        project: Path,
        failure: Exception | None = None,
        during: Callable[[WorkerDirective], None] | None = None,
    ) -> None:
        self.project, self.failure, self.during = project, failure, during
        self.directives: list[WorkerDirective] = []

    async def run(self, directive: WorkerDirective, backend: WorkerBackend) -> None:
        self.directives.append(directive)
        await backend.run(AgentRequest(
            "research-test", directive.worker_id, directive.role, "Proof worker prompt.", None,
            self.project, self.project, "session", self.project / ".aizim/run/gateway.sock",
            directive.timeout_seconds,
        ))
        if self.during is not None:
            self.during(directive)
        if self.failure is not None:
            raise self.failure


def audit(state: StateService, run_id: str, worker_id: str, *outcomes: tuple[str, str]) -> None:
    """Record gateway outcomes for one worker as the capability gateway does."""
    for number, (event_type, code) in enumerate(outcomes):
        payload: dict[str, JsonValue] = {
            "reason_code": code,
            "role": "proof_explorer",
            "worker_id": worker_id,
            "operation": "document.apply",
            "request_id": f"request-{number}",
        }
        if event_type == "GatewayCallFailed":
            payload["error_type"] = "WorkerGatewayError"
        state.append_event(
            AppendEventCommand(event_type, "capability_gateway", run_id, None, payload)
        )


async def run_one_round(
    project: Path,
    run_id: str,
    backend: ScriptedBackend,
    failure: Exception | None = None,
    outcomes: tuple[tuple[str, str], ...] = (),
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    """Run one LeanExecutor round whose worker stops with ``failure`` after ``outcomes``."""
    with StateService(StateServiceConfig(project, run_id)) as state:
        define_task(state, {**task(), "max_rounds": 1})
        host = DirectHost(
            project,
            failure,
            lambda directive: audit(state, run_id, directive.worker_id, *outcomes),
        )
        executor = LeanExecutor(state, project, host, backend)
        async def execute(target, attempt, context):
            result = await executor(target, attempt, context)
            engine.stop.set()
            return result

        engine = ResearchEngine(state, execute, run_id, "test-model")
        await engine.run()
        attempt, = records(state, "attempt")
        return attempt, require_task(state, "addition")


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
    """Persist the source and dependency evidence used to settle and recover attempts."""
    submission = submit(state, attempt)
    publish(state, submission)
    return AttemptResult(
        "Verified fixture evidence.",
        (text(submission["declaration_id"]),),
        {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10},
    )


def submit(state: StateService, attempt) -> dict[str, JsonValue]:
    """Submit the attempt's proof and return the publication that would settle it."""
    cid, did = "contribution-" + attempt["id"], "declaration-" + attempt["id"]
    target = require_task(state, attempt["task_id"])
    run_id, worker_id = text(attempt["run_id"]), text(attempt["worker_id"])
    name = candidate_name(run_id, worker_id)
    statement = text(target["statement"])
    imports = texts(target["imports"])
    for dependency in texts(target["depends_on"]):
        parent = require_task(state, dependency)
        for declaration in texts(parent["verified_declarations"]):
            projection = state.query_projection("verified_declarations", declaration)
            assert projection is not None
            imports.append(text(json.loads(projection.state_json)["payload"]["module"]))
    imports = list(dict.fromkeys(imports))
    separator = "" if statement.startswith(("(", "{", "[")) else ": "
    source = (
        "".join(f"import {module}\n" for module in imports)
        + f"\ntheorem {name} {separator}{statement} := by\n  rfl\n"
    ).encode()
    project = state.socket_path.parents[2]
    artifacts = ArtifactStore(project)
    contribution = artifacts.store(run_id, "contributions", source, "text/x-lean")
    publication = artifacts.store(run_id, "promotions", source, "text/x-lean")
    module = f"AizimResearch.{name}"
    published_path = project / module_path(module)
    published_path.parent.mkdir(parents=True, exist_ok=True)
    published_path.write_bytes(source)
    payload: dict[str, JsonValue] = {
        "contribution_id": cid,
        "worker_id": worker_id,
        "candidate_name": name,
        "complete_type": statement,
        "payload_hash": contribution.content_hash,
        "imports": [*imports],
    }
    state.append_event(
        AppendEventCommand(
            "ContributionSubmitted",
            "fixture",
            attempt["run_id"],
            None,
            payload,
        )
    )
    return {
        "run_id": attempt["run_id"],
        "declaration_id": did,
        "contribution_id": cid,
        "module": module,
        "content_hash": publication.content_hash,
        "name": name,
        "type": statement,
    }


def publish(state: StateService, submission: dict[str, JsonValue]) -> None:
    run_id = text(submission["run_id"])
    payload = {key: value for key, value in submission.items() if key != "run_id"}
    state.append_event(AppendEventCommand("DeclarationPublished", "fixture", run_id, None, payload))


async def never(*args: object) -> AttemptResult:
    raise AssertionError("this round must not execute")


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


@pytest.mark.parametrize("with_dependency", [False, True])
async def test_recovery_settles_an_already_published_attempt_without_reexecuting(
    project: Path,
    with_dependency: bool,
) -> None:
    with StateService(StateServiceConfig(project, "recovery-test")) as state:
        define_task(state, task())

        async def never(*args):
            raise AssertionError("published work must not execute again")

        engine = ResearchEngine(state, never, "previous-run", "test-model")
        target_id = "addition"
        if with_dependency:
            define_task(state, task("consequence", ["addition"]))
            dependency = engine._begin(require_task(state, "addition"))
            engine._finish(dependency, accepted(state, dependency))
            target_id = "consequence"
        attempt = engine._begin(require_task(state, target_id))
        accepted(state, attempt)
    with StateService(StateServiceConfig(project, "recovery-restart")) as state:
        await ResearchEngine(state, never, "next-run", "test-model").run()
        assert require_task(state, "addition")["status"] == "verified"
        assert require_task(state, target_id)["status"] == "verified"
        assert len(records(state, "attempt")) == (2 if with_dependency else 1)
        assert state.replay_verify().matched


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


def test_usage_price_distinguishes_cached_input() -> None:
    assert estimate(
        {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 100},
        {
            "input_per_million": "10",
            "cached_input_per_million": "1",
            "output_per_million": "50",
        },
    ) == "0.013200"


def test_static_dashboard_safely_embeds_research_text() -> None:
    dangerous = "</script><script>alert('research')</script> & ∀"
    snapshot: dict[str, JsonValue] = {
        "project": dangerous,
        "records": {"task": [{"statement": dangerous}]},
    }
    page = document(snapshot)
    assert dangerous not in page
    assert "\\u003c/script\\u003e" in page
    embedded = page.split("const initial=", 1)[1].split(";\n", 1)[0]
    assert json.loads(embedded) == snapshot


async def test_a_stopped_worker_round_keeps_its_report_usage_and_stop_reason(
    project: Path, private_tmpdir: Path
) -> None:
    attempt, target = await run_one_round(
        project, "stopped-round-test", ScriptedBackend(), WorkerExecutionError("WORKER_ABSTAINED")
    )

    assert attempt["status"] == "failed"
    assert "WORKER_ABSTAINED" in text(attempt["feedback"])
    assert "Tried simp; the goal n + 0 = n remained." in text(attempt["feedback"])
    assert attempt["usage"] == {"input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 5}
    assert attempt["model"] == "magpie/test"
    assert target["failures"] == 1


async def test_gateway_errors_of_a_round_are_summarized_for_the_next_round(
    project: Path, private_tmpdir: Path
) -> None:
    run_id = "tool-errors-test"
    holder: dict[str, StateService] = {}

    def refuse(directive: WorkerDirective) -> None:
        audit(
            holder["state"], run_id, directive.worker_id,
            ("GatewayCallFailed", "UNVERIFIED_DOCUMENT_APPLY"),
            ("GatewayCallFailed", "UNVERIFIED_DOCUMENT_APPLY"),
            ("CapabilityDenied", "WORKER_BUDGET_EXHAUSTED"),
        )
        audit(holder["state"], run_id, "another-worker", ("GatewayCallFailed", "LEASE_REQUIRED"))

    with StateService(StateServiceConfig(project, run_id)) as state:
        holder["state"] = state
        define_task(state, {**task(), "max_rounds": 1})
        host = DirectHost(project, WorkerExecutionError("BACKEND_FAILED"), refuse)
        executor = LeanExecutor(state, project, host, ScriptedBackend())
        await ResearchEngine(state, executor, run_id, "test-model").run()
        attempt, = records(state, "attempt")

    feedback = text(attempt["feedback"])
    assert "UNVERIFIED_DOCUMENT_APPLY (2)" in feedback
    assert "WORKER_BUDGET_EXHAUSTED (1)" in feedback
    assert "LEASE_REQUIRED" not in feedback


async def test_malformed_backend_usage_is_dropped_instead_of_failing_the_engine(
    project: Path, private_tmpdir: Path
) -> None:
    backend = ScriptedBackend(usage={"input_tokens": 5, "output_tokens": 1})
    attempt, _target = await run_one_round(project, "usage-test", backend)

    assert attempt["status"] == "failed"
    assert attempt["usage"] is None


@pytest.mark.parametrize(
    ("error", "feedback"),
    [
        (
            RuntimeError("OMP_MODEL_UNAVAILABLE"),
            "Round failed: RuntimeError: OMP_MODEL_UNAVAILABLE.",
        ),
        (RuntimeError("/private/path/secret.lean"), "Round failed: RuntimeError."),
    ],
)
async def test_unexpected_round_errors_report_only_their_reason_code(
    project: Path, error: Exception, feedback: str
) -> None:
    with StateService(StateServiceConfig(project, "round-error-test")) as state:
        define_task(state, {**task(), "max_rounds": 1})

        async def execute(*args: object) -> AttemptResult:
            raise error

        await ResearchEngine(state, execute, "round-error-test", "test-model").run()
        attempts = records(state, "attempt")

    assert all(attempt["feedback"] == feedback for attempt in attempts)


async def test_a_publication_after_the_round_closed_still_verifies_the_target(
    project: Path,
) -> None:
    submissions: dict[int, dict[str, JsonValue]] = {}
    with StateService(StateServiceConfig(project, "late-publication-test")) as state:
        define_task(state, task())

        async def execute(target, attempt, context):
            if attempt["round"] == 1:
                submissions[1] = submit(state, attempt)
                return AttemptResult("Publication did not finish within the round deadline.")
            publish(state, submissions[1])
            return AttemptResult("No new proof in this round.")

        await ResearchEngine(state, execute, "late-publication-test", "test-model").run()
        target = require_task(state, "addition")
        attempts = sorted(records(state, "attempt"), key=lambda entry: integer(entry["round"]))
        assert state.replay_verify().matched

    assert target["status"] == "verified"
    assert target["verified_declarations"] == [submissions[1]["declaration_id"]]
    assert [entry["status"] for entry in attempts] == ["verified", "failed"]
    assert "awaiting_publication" not in attempts[0]


async def test_recovery_verifies_a_target_published_after_its_round_closed(project: Path) -> None:
    with StateService(StateServiceConfig(project, "late-recovery-test")) as state:
        define_task(state, task())
        engine = ResearchEngine(state, never, "late-recovery-test", "test-model")
        attempt = engine._begin(require_task(state, "addition"))
        submission = submit(state, attempt)
        engine._finish(attempt, AttemptResult("Publication did not finish."))
        waiting = get(state, "attempt", text(attempt["id"]))
        assert waiting is not None and waiting["awaiting_publication"] is True
        publish(state, submission)
    with StateService(StateServiceConfig(project, "late-recovery-restart")) as state:
        await ResearchEngine(state, never, "late-recovery-restart", "test-model").run()
        target = require_task(state, "addition")

    assert target["status"] == "verified"
    assert target["verified_declarations"] == [submission["declaration_id"]]


async def test_a_run_waits_for_its_last_submission_to_publish(project: Path) -> None:
    run_id = "final-publication-test"
    with StateService(StateServiceConfig(project, run_id)) as state:
        define_task(state, {**task(), "max_rounds": 1})
        promotions: list[asyncio.Task[None]] = []

        async def promote(submission: dict[str, JsonValue]) -> None:
            await asyncio.sleep(0.3)
            publish(state, submission)

        async def execute(target, attempt, context):
            promotions.append(asyncio.create_task(promote(submit(state, attempt))))
            return AttemptResult("Publication did not finish within the round deadline.")

        # Closing the host after the run would cancel a promotion that is still running.
        await ResearchEngine(state, execute, run_id, "test-model").run()
        settled = all(promotion.done() for promotion in promotions)
        target = require_task(state, "addition")

    assert settled
    assert target["status"] == "verified"


def test_a_rebased_submission_settles_with_its_successor(project: Path) -> None:
    run_id = "rebase-settle-test"
    with StateService(StateServiceConfig(project, run_id)) as state:
        define_task(state, task())
        engine = ResearchEngine(state, never, run_id, "test-model")
        attempt = engine._begin(require_task(state, "addition"))
        original = text(submit(state, attempt)["contribution_id"])
        engine._finish(attempt, AttemptResult("Publication did not finish."))
        payload = next(
            thaw_payload(record.envelope.payload)
            for record in state.query_events(run_id)
            if record.envelope.event_type == "ContributionSubmitted"
        )
        # Promotion found the base stale and resubmitted the proof; Lean then rejected it.
        successor = "contribution-rebased"
        promotion: list[tuple[str, dict[str, JsonValue]]] = [
            ("ContributionSubmitted", {**payload, "contribution_id": successor}),
            (
                "ContributionRebased",
                {"contribution_id": successor, "source_contribution_id": original},
            ),
            ("PromotionFailed", {"contribution_id": successor, "reason_code": "LEAN_FAILED"}),
        ]
        for event_type, body in promotion:
            state.append_event(AppendEventCommand(event_type, "promotion", run_id, None, body))
        engine._reconcile()
        settled = get(state, "attempt", text(attempt["id"]))

    assert settled is not None and settled["status"] == "failed"
    assert "awaiting_publication" not in settled


def test_verifying_a_paused_target_lifts_its_pause(project: Path) -> None:
    run_id = "verified-pause-test"
    names = ("in-round", "late")
    with StateService(StateServiceConfig(project, run_id)) as state:
        engine = ResearchEngine(state, never, run_id, "test-model")
        for identity in names:
            define_task(state, task(identity))
        # One target verifies within its round and one after it; both were paused meanwhile.
        in_round = engine._begin(require_task(state, "in-round"))
        late = engine._begin(require_task(state, "late"))
        submission = submit(state, late)
        engine._finish(late, AttemptResult("Publication did not finish."))
        for identity in names:
            pause: dict[str, JsonValue] = {"task_id": identity, "author": "Ada"}
            operator_change(state, {"action": "task.pause", "body": pause})
        engine._finish(in_round, accepted(state, in_round))
        publish(state, submission)
        engine._reconcile()
        targets = [require_task(state, identity) for identity in names]

    assert [(target["status"], target["paused"]) for target in targets] == [
        ("verified", False),
        ("verified", False),
    ]


async def test_a_worker_that_uses_its_whole_timeout_still_gets_the_publication_wait(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aizim.research.engine.PUBLICATION_WAIT_SECONDS", 0.5)
    run_id = "slow-worker-test"
    with StateService(StateServiceConfig(project, run_id)) as state:
        define_task(state, {**task(), "timeout_seconds": 1, "max_rounds": 1})

        class SlowStartHost:
            """Spend the whole worker timeout plus start-up time, then submit."""

            async def run(self, directive: WorkerDirective, backend: WorkerBackend) -> None:
                await asyncio.sleep(directive.timeout_seconds + 0.1)
                submit(state, records(state, "attempt")[0])

        executor = LeanExecutor(state, project, SlowStartHost(), ScriptedBackend())
        await ResearchEngine(state, executor, run_id, "test-model").run()
        attempt, = records(state, "attempt")
        target = require_task(state, "addition")

    assert text(attempt["feedback"]).startswith("Publication did not finish")
    assert (target["failures"], target.get("infrastructure_failures")) == (1, None)


def test_a_later_round_cannot_downgrade_a_verified_target(project: Path) -> None:
    with StateService(StateServiceConfig(project, "downgrade-test")) as state:
        define_task(state, task())
        engine = ResearchEngine(state, never, "downgrade-test", "test-model")
        first = engine._begin(require_task(state, "addition"))
        second = engine._begin(require_task(state, "addition"))
        engine._finish(first, accepted(state, first))
        engine._finish(second, AttemptResult("No proof in this round."))
        target = require_task(state, "addition")
        later = get(state, "attempt", text(second["id"]))

    assert target["status"] == "verified"
    assert target["verified_declarations"] == ["declaration-" + text(first["id"])]
    assert target["failures"] == 0
    assert later is not None and later["status"] == "failed"


async def test_infrastructure_failures_spare_the_failure_budget_and_pause_the_task(
    project: Path,
) -> None:
    with StateService(StateServiceConfig(project, "infrastructure-test")) as state:
        define_task(state, {**task(), "max_failures": 1, "max_rounds": 1})

        async def execute(*args: object) -> AttemptResult:
            return AttemptResult(
                "Round failed: OmpBackendError: OMP_MODEL_UNAVAILABLE.",
                infrastructure_failure=True,
            )

        await ResearchEngine(state, execute, "infrastructure-test", "test-model").run()
        paused = require_task(state, "addition")
        operator_change(
            state, {"action": "task.resume", "body": {"task_id": "addition", "author": "Ada"}}
        )
        resumed = require_task(state, "addition")

    assert paused["failures"] == 0
    assert paused["rounds"] == 0
    assert paused["attempt_sequence"] == 3
    assert paused["status"] == "pending" and paused["paused"] is True
    assert paused["infrastructure_failures"] == 3
    assert "infrastructure failures" in text(paused["feedback"])
    assert resumed["paused"] is False
    assert "infrastructure_failures" not in resumed


async def test_a_proof_failure_resets_the_infrastructure_streak(project: Path) -> None:
    outcomes = iter((True, True, False))
    with StateService(StateServiceConfig(project, "streak-test")) as state:
        define_task(state, {**task(), "max_rounds": 1})

        async def execute(*args: object) -> AttemptResult:
            infrastructure = next(outcomes)
            return AttemptResult("Round outcome.", infrastructure_failure=infrastructure)

        await ResearchEngine(state, execute, "streak-test", "test-model").run()
        target = require_task(state, "addition")

    assert target["failures"] == 1
    assert "infrastructure_failures" not in target
    assert target["paused"] is False
    assert target["status"] == "blocked"


async def test_a_blocked_task_resumes_once_its_limits_allow_another_round(project: Path) -> None:
    with StateService(StateServiceConfig(project, "resume-test")) as state:
        define_task(state, {**task(), "max_failures": 1})

        async def execute(*args: object) -> AttemptResult:
            return AttemptResult("No proof in this round.")

        await ResearchEngine(state, execute, "resume-test", "test-model").run()
        assert require_task(state, "addition")["status"] == "blocked"
        resume: dict[str, JsonValue] = {"task_id": "addition", "author": "Ada"}
        with pytest.raises(ResearchError, match="TASK_LIMIT_REACHED_RAISE_LIMITS_TO_RESUME"):
            operator_change(state, {"action": "task.resume", "body": resume})
        for invalid in (0, 1001, True, "2"):
            with pytest.raises(ResearchError, match="INVALID_INTEGER"):
                operator_change(
                    state, {"action": "task.resume", "body": {**resume, "max_failures": invalid}}
                )
        with pytest.raises(ResearchError, match="INVALID_OPERATOR_REQUEST"):
            operator_change(state, {"action": "task.pause", "body": {**resume, "max_failures": 2}})
        operator_change(
            state, {"action": "task.resume", "body": {**resume, "max_failures": 2, "max_rounds": 9}}
        )
        target = require_task(state, "addition")
        ready = [entry["task_id"] for entry in frontier(state)]

    assert target["status"] == "pending"
    assert (target["max_failures"], target["max_rounds"]) == (2, 9)
    assert ready == ["addition"]


def test_a_task_paused_during_its_final_round_can_resume(project: Path) -> None:
    resume: dict[str, JsonValue] = {"task_id": "addition", "author": "Ada"}
    with StateService(StateServiceConfig(project, "resume-running-test")) as state:
        define_task(state, {**task(), "max_rounds": 1})
        engine = ResearchEngine(state, never, "resume-running-test", "test-model")
        engine._begin(require_task(state, "addition"))
        operator_change(state, {"action": "task.pause", "body": resume})
        operator_change(state, {"action": "task.resume", "body": resume})
        target = require_task(state, "addition")

    assert (target["status"], target["paused"]) == ("running", False)


def test_resume_cannot_lower_limits_below_what_the_task_has_used(project: Path) -> None:
    resume: dict[str, JsonValue] = {"task_id": "addition", "author": "Ada"}
    with StateService(StateServiceConfig(project, "resume-lower-test")) as state:
        define_task(state, task())
        engine = ResearchEngine(state, never, "resume-lower-test", "test-model")
        engine._finish(engine._begin(require_task(state, "addition")), AttemptResult("No proof."))
        operator_change(state, {"action": "task.pause", "body": resume})
        with pytest.raises(ResearchError, match="TASK_LIMIT_REACHED_RAISE_LIMITS_TO_RESUME"):
            operator_change(
                state, {"action": "task.resume", "body": {**resume, "max_failures": 1}}
            )
        target = require_task(state, "addition")

    assert (target["status"], target["paused"], target["max_failures"]) == ("pending", True, 3)


@pytest.mark.parametrize(
    ("stop", "outcomes", "failures", "streak"),
    [
        ("BACKEND_FAILED", (), 0, 1),
        ("BACKEND_FAILED", (("CapabilityDenied", "WORKER_BUDGET_EXHAUSTED"),), 1, None),
        ("WORKER_TIMEOUT", (), 1, None),
        ("WORKER_ABSTAINED", (("GatewayCallFailed", "UNVERIFIED_DOCUMENT_APPLY"),), 1, None),
    ],
)
async def test_only_faults_outside_the_attempt_count_as_infrastructure(
    project: Path,
    private_tmpdir: Path,
    stop: str,
    outcomes: tuple[tuple[str, str], ...],
    failures: int,
    streak: int | None,
) -> None:
    _attempt, target = await run_one_round(
        project, "classification-test", ScriptedBackend(), WorkerExecutionError(stop), outcomes
    )

    assert target["failures"] == failures
    assert target.get("infrastructure_failures") == streak


async def test_run_circuit_breaker_samples_three_targets_and_stops_watch(project: Path) -> None:
    seen: list[str] = []
    with StateService(StateServiceConfig(project, "run-outage-test")) as state:
        for identity in ("alpha", "beta", "gamma", "omega"):
            define_task(state, {**task(identity), "max_rounds": 1})

        async def outage(target, attempt, context):
            seen.append(text(target["task_id"]))
            return AttemptResult("Model unavailable.", infrastructure_failure=True)

        engine = ResearchEngine(state, outage, "run-outage-test", "test-model", workers=1)
        await asyncio.wait_for(engine.run(watch=True), timeout=5)
        assert seen == ["alpha", "beta", "gamma"]
        assert engine.reason == "infrastructure_unavailable"
        assert not engine.active
        assert require_task(state, "omega")["attempt_sequence"] == 0
        assert all(target["rounds"] == 0 for target in records(state, "task"))
        assert all(entry["charged"] is False for entry in records(state, "attempt"))
        assert state.replay_verify().matched


async def test_outage_retry_preserves_round_allowance_and_monotonic_sequence(project: Path) -> None:
    with StateService(StateServiceConfig(project, "outage-retry-test")) as state:
        define_task(state, {**task(), "max_rounds": 1})

        async def execute(target, attempt, context):
            if attempt["round"] == 1:
                return AttemptResult("Temporary outage.", infrastructure_failure=True)
            return accepted(state, attempt)

        engine = ResearchEngine(state, execute, "outage-retry-test", "test-model", workers=1)
        await engine.run()
        target = require_task(state, "addition")
        assert (target["status"], target["rounds"], target["attempt_sequence"]) == (
            "verified", 1, 2,
        )
        attempts = sorted(records(state, "attempt"), key=lambda item: integer(item["round"]))
        assert [entry["charged"] for entry in attempts] == [False, True]
        assert engine.reason == ""
        assert state.replay_verify().matched


async def test_restart_refunds_interrupted_round_and_keeps_sequence(project: Path) -> None:
    with StateService(StateServiceConfig(project, "interrupted-sequence-test")) as state:
        define_task(state, {**task(), "max_rounds": 1})
        ResearchEngine(state, never, "interrupted-sequence-test", "test-model")._begin(
            require_task(state, "addition")
        )

    with StateService(StateServiceConfig(project, "sequence-restart")) as state:
        async def execute(target, attempt, context):
            assert attempt["round"] == 2
            return accepted(state, attempt)

        await ResearchEngine(state, execute, "sequence-restart", "test-model").run()
        target = require_task(state, "addition")
        assert (target["status"], target["rounds"], target["attempt_sequence"]) == (
            "verified", 1, 2,
        )
        interrupted = [
            entry for entry in records(state, "attempt") if entry["status"] == "interrupted"
        ]
        assert len(interrupted) == 1 and interrupted[0]["charged"] is False
        assert state.replay_verify().matched


async def test_throttled_gateway_calls_are_audited_but_not_reported_as_refusals(
    project: Path, private_tmpdir: Path
) -> None:
    attempt, target = await run_one_round(
        project, "rate-limit-report-test", ScriptedBackend(),
        WorkerExecutionError("WORKER_ABSTAINED"),
        (("CapabilityDenied", "REQUEST_RATE_EXCEEDED"),),
    )
    assert "Gateway refusals" not in text(attempt["feedback"])
    assert target["rounds"] == 1 and target["failures"] == 1


async def test_repeated_crashes_during_recovery_do_not_strand_refunded_target(
    project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Crash(BaseException):
        pass

    def crash_before_task_save(store, kind, identity, data, **kwargs):
        if kind == "task":
            raise Crash
        return save(store, kind, identity, data, **kwargs)

    def crash_before_refund_save(store, kind, identity, data, **kwargs):
        if kind == "task" and data["status"] != "running":
            raise Crash
        return save(store, kind, identity, data, **kwargs)

    with StateService(StateServiceConfig(project, "orphaned-attempt")) as state:
        define_task(state, {**task(), "max_rounds": 1})
        engine = ResearchEngine(state, never, "orphaned-attempt", "test-model")
        with monkeypatch.context() as patch:
            patch.setattr("aizim.research.engine.save", crash_before_task_save)
            with pytest.raises(Crash):
                engine._begin(require_task(state, "addition"))

    with StateService(StateServiceConfig(project, "interrupted-recovery")) as state:
        engine = ResearchEngine(state, never, "interrupted-recovery", "test-model")
        with monkeypatch.context() as patch:
            patch.setattr("aizim.research.engine.save", crash_before_refund_save)
            with pytest.raises(Crash):
                engine.recover()

    with StateService(StateServiceConfig(project, "completed-recovery")) as state:
        async def execute(target, attempt, context):
            assert attempt["round"] == 2
            return accepted(state, attempt)

        engine = ResearchEngine(state, execute, "completed-recovery", "test-model")
        await engine.run()
        target = require_task(state, "addition")
        assert target["status"] == "verified" and target["rounds"] == 1
        assert state.replay_verify().matched


@pytest.mark.parametrize("interrupted", [False, True])
def test_late_verification_preserves_refund_of_active_attempt(
    project: Path, interrupted: bool,
) -> None:
    with StateService(StateServiceConfig(project, "verified-refund")) as state:
        define_task(state, task())
        engine = ResearchEngine(state, never, "verified-refund", "test-model")
        first = engine._begin(require_task(state, "addition"))
        submission = submit(state, first)
        engine._finish(first, AttemptResult("Publication still pending."))
        second = engine._begin(require_task(state, "addition"))
        publish(state, submission)
        engine._reconcile()
        assert require_task(state, "addition")["rounds"] == 2
        engine._finish(
            second, AttemptResult("Stopped.", infrastructure_failure=not interrupted),
            interrupted=interrupted,
        )
        target = require_task(state, "addition")
        assert target["status"] == "verified" and target["rounds"] == 1
        assert target["failures"] == 0
        assert target["verified_declarations"] == [submission["declaration_id"]]
        assert state.replay_verify().matched
