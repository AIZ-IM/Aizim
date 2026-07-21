from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aizim.state import (
    AppendEventCommand,
    DuplicateEventError,
    EventValidationError,
    IncompatibleEventSchemaError,
    ProjectionRecord,
    StateDependencies,
    StateService,
    StateServiceConfig,
    apply_event,
)
from aizim.state.events import EventEnvelope


class SequenceValues[T]:
    def __init__(self, values: tuple[T, ...]) -> None:
        self._values = iter(values)

    def __call__(self) -> T:
        return next(self._values)


class ProjectionFailure(RuntimeError):
    pass


def _dependencies(
    event_ids: tuple[str, ...],
    times: tuple[datetime, ...],
    reducer: Callable[
        [tuple[ProjectionRecord, ...], EventEnvelope], tuple[ProjectionRecord, ...]
    ] = apply_event,
) -> StateDependencies:
    return StateDependencies(
        clock=SequenceValues(times),
        event_ids=SequenceValues(event_ids),
        reducer=reducer,
    )


def _config(project_root: Path) -> StateServiceConfig:
    return StateServiceConfig(project_root=project_root, service_session="service-session")


def _project_command(project_id: str = "project-1") -> AppendEventCommand:
    return AppendEventCommand(
        event_type="ProjectInitialized",
        actor="supervisor",
        run_id=None,
        causation_id=None,
        payload={
            "project_id": project_id,
            "base_epoch": "a" * 64,
            "knowledge_epoch": 0,
        },
    )


def test_initialization_configures_sqlite_and_empty_epoch(tmp_path: Path) -> None:
    # Given / When
    with StateService(_config(tmp_path)) as service:
        health = service.health()
        epoch = service.query_projection("epochs", "global")

    # Then
    assert health.journal_mode == "wal"
    assert health.foreign_keys
    assert health.synchronous == "full"
    assert health.busy_timeout_ms == 5000
    assert health.event_schema_version == 1
    assert epoch is not None
    assert epoch.version == 0
    assert epoch.state_json == b'{"knowledge_epoch":0}'


def test_projection_failure_rolls_back_event_and_projection(tmp_path: Path) -> None:
    # Given
    def fail_projection(
        snapshots: tuple[ProjectionRecord, ...], event: EventEnvelope
    ) -> tuple[ProjectionRecord, ...]:
        del snapshots, event
        raise ProjectionFailure

    dependencies = _dependencies(
        ("01J00000000000000000000000",),
        (datetime(2026, 7, 21, 10, tzinfo=UTC),),
        fail_projection,
    )

    # When
    with StateService(_config(tmp_path), dependencies) as service:
        with pytest.raises(ProjectionFailure):
            service.append_event(_project_command())

        # Then
        assert service.query_events() == ()
        assert service.query_projection("project", "project-1") is None


def test_duplicate_event_id_does_not_change_projection(tmp_path: Path) -> None:
    # Given
    event_id = "01J00000000000000000000000"
    dependencies = _dependencies(
        (event_id, event_id),
        (
            datetime(2026, 7, 21, 10, tzinfo=UTC),
            datetime(2026, 7, 21, 11, tzinfo=UTC),
        ),
    )

    with StateService(_config(tmp_path), dependencies) as service:
        service.append_event(_project_command())
        original = service.query_projection("project", "project-1")

        # When
        with pytest.raises(DuplicateEventError):
            service.append_event(_project_command("project-2"))

        # Then
        assert len(service.query_events()) == 1
        assert service.query_projection("project", "project-1") == original
        assert service.query_projection("project", "project-2") is None


def test_events_follow_database_sequence_not_timestamp(tmp_path: Path) -> None:
    # Given
    dependencies = _dependencies(
        (
            "01J00000000000000000000000",
            "01J00000000000000000000001",
        ),
        (
            datetime(2026, 7, 21, 11, tzinfo=UTC),
            datetime(2026, 7, 21, 10, tzinfo=UTC),
        ),
    )

    with StateService(_config(tmp_path), dependencies) as service:
        # When
        service.append_event(_project_command())
        service.append_event(
            AppendEventCommand(
                event_type="RunCreated",
                actor="supervisor",
                run_id="run-1",
                causation_id=None,
                payload={},
            )
        )
        events = service.query_events()

    # Then
    assert tuple(record.sequence for record in events) == (1, 2)
    assert events[0].envelope.occurred_at > events[1].envelope.occurred_at


def test_future_event_schema_fails_startup(tmp_path: Path) -> None:
    # Given
    with StateService(_config(tmp_path)):
        pass
    database = tmp_path / ".aizim" / "state.sqlite3"
    subprocess.run(
        [
            "/usr/bin/sqlite3",
            database,
            (
                "INSERT INTO events(event_id,schema_version,event_type,occurred_at,actor,"
                "run_id,causation_id,payload_json) VALUES"
                "('01J00000000000000000000002',2,'RunCreated',"
                "'2026-07-21T10:00:00Z','supervisor','run-1',NULL,'{}')"
            ),
        ],
        check=True,
    )

    # When / Then
    with pytest.raises(IncompatibleEventSchemaError, match="INCOMPATIBLE_EVENT_SCHEMA"):
        StateService(_config(tmp_path))


def test_unknown_schema_v1_payload_field_fails_startup(tmp_path: Path) -> None:
    # Given
    with StateService(_config(tmp_path)):
        pass
    database = tmp_path / ".aizim" / "state.sqlite3"
    payload = (
        '{"base_epoch":"'
        + "a" * 64
        + '","knowledge_epoch":0,"project_id":"project-1","unexpected":true}'
    )
    subprocess.run(
        [
            "/usr/bin/sqlite3",
            database,
            (
                "INSERT INTO events(event_id,schema_version,event_type,occurred_at,actor,"
                "run_id,causation_id,payload_json) VALUES"
                "('01J00000000000000000000002',1,'ProjectInitialized',"
                f"'2026-07-21T10:00:00Z','supervisor',NULL,NULL,'{payload}')"
            ),
        ],
        check=True,
    )

    # When / Then
    with pytest.raises(EventValidationError):
        StateService(_config(tmp_path))


def test_keyboard_interrupt_cannot_expose_partial_write(tmp_path: Path) -> None:
    # Given
    def interrupt_projection(
        snapshots: tuple[ProjectionRecord, ...], event: EventEnvelope
    ) -> tuple[ProjectionRecord, ...]:
        del snapshots, event
        raise KeyboardInterrupt

    dependencies = _dependencies(
        ("01J00000000000000000000000",),
        (datetime(2026, 7, 21, 10, tzinfo=UTC),),
        interrupt_projection,
    )
    # When
    with StateService(_config(tmp_path), dependencies) as service, pytest.raises(
        KeyboardInterrupt
    ):
        service.append_event(_project_command())

    # Then
    with StateService(_config(tmp_path)) as restarted:
        assert restarted.query_events() == ()
        assert restarted.query_projection("project", "project-1") is None


def test_sandbox_probe_audit_events_do_not_change_protected_digest(
    tmp_path: Path,
) -> None:
    with StateService(_config(tmp_path)) as service:
        before = service.logical_digest()
        service.append_event(
            AppendEventCommand(
                "SandboxProbeStarted",
                "sandbox_adapter",
                "run-1",
                None,
                {"probe_id": "probe-1:start", "profile": "aizim-worker"},
            )
        )
        service.append_event(
            AppendEventCommand(
                "SandboxProbeDenied",
                "sandbox_adapter",
                "run-1",
                None,
                {
                    "probe_id": "probe-1:read_state_database",
                    "reason_code": "SANDBOX_ENFORCED",
                    "operation": "read_state_database",
                },
            )
        )
        service.append_event(
            AppendEventCommand(
                "SandboxProbePassed",
                "sandbox_adapter",
                "run-1",
                None,
                {
                    "probe_id": "probe-1:read_allowed_view",
                    "operation": "read_allowed_view",
                },
            )
        )
        service.append_event(
            AppendEventCommand(
                "SandboxProbeFailed",
                "sandbox_adapter",
                "run-1",
                None,
                {"probe_id": "probe-1:failed", "reason_code": "PROBE_FAILED"},
            )
        )

        assert service.logical_digest() == before
        assert service.replay_verify().matched
