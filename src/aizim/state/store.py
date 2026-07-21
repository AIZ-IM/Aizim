from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from aizim.domain.serialization import JsonValue, canonical_json, sha256_bytes

from .events import (
    EVENT_SCHEMA_VERSION,
    EventEnvelope,
    EventValidationError,
    IncompatibleEventSchemaError,
    event_as_dict,
    upcast,
)
from .projections import (
    AUDIT_PROJECTIONS,
    PROJECTION_NAMES,
    ProjectionRecord,
    ProjectionReducer,
)

_SCHEMA_PATH: Final = Path(__file__).with_name("sql") / "001_foundation.sql"


@dataclass(frozen=True, slots=True)
class DuplicateEventError(RuntimeError):
    event_id: str

    def __str__(self) -> str:
        return f"duplicate event id: {self.event_id}"


@dataclass(frozen=True, slots=True)
class ProjectionAuthorityError(RuntimeError):
    projection_name: str
    reason: str

    def __str__(self) -> str:
        return f"projection {self.projection_name}: {self.reason}"


@dataclass(frozen=True, slots=True)
class StoreHealth:
    journal_mode: str
    foreign_keys: bool
    synchronous: str
    busy_timeout_ms: int
    event_schema_version: int


@dataclass(frozen=True, slots=True)
class EventRecord:
    sequence: int
    envelope: EventEnvelope


@dataclass(frozen=True, slots=True)
class ReplayVerification:
    matched: bool
    projection_json: bytes
    logical_digest: str


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialized = False
    try:
        _initialize(connection)
        initialized = True
        return connection
    finally:
        if not initialized:
            connection.close()


def _initialize(connection: sqlite3.Connection) -> None:
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if user_version == 0:
        connection.executescript(_SCHEMA_PATH.read_text())
        connection.execute(f"PRAGMA user_version={EVENT_SCHEMA_VERSION}")
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            connection.execute(
                "INSERT INTO metadata(key,value_json) VALUES(?,?)",
                ("event_schema_version", str(EVENT_SCHEMA_VERSION)),
            )
            connection.execute(
                "INSERT INTO projections(projection_name,entity_id,version,state_json) "
                "VALUES(?,?,?,?)",
                ("epochs", "global", 0, canonical_json({"knowledge_epoch": 0}).decode()),
            )
    elif user_version != EVENT_SCHEMA_VERSION:
        raise IncompatibleEventSchemaError(user_version)
    metadata = connection.execute(
        "SELECT value_json FROM metadata WHERE key=?", ("event_schema_version",)
    ).fetchone()
    if metadata is None or metadata[0] != str(EVENT_SCHEMA_VERSION):
        raise IncompatibleEventSchemaError(-1 if metadata is None else int(metadata[0]))
    future = connection.execute(
        "SELECT MAX(schema_version) FROM events WHERE schema_version > ?",
        (EVENT_SCHEMA_VERSION,),
    ).fetchone()[0]
    if future is not None:
        raise IncompatibleEventSchemaError(future)
    _query_events(connection)


def _event_record(
    row: tuple[int, str, int, str, str, str, str | None, str | None, str],
) -> EventRecord:
    sequence, event_id, version, event_type, occurred_at, actor, run_id, causation_id, raw = row
    payload: JsonValue = json.loads(raw)
    if type(payload) is not dict:
        raise EventValidationError("payload", "stored payload must be a JSON object")
    document: dict[str, JsonValue] = {
        "event_id": event_id,
        "schema_version": version,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "actor": actor,
        "run_id": run_id,
        "causation_id": causation_id,
        "payload": payload,
    }
    return EventRecord(sequence=sequence, envelope=upcast(document))


def _query_events(
    connection: sqlite3.Connection, run_id: str | None = None
) -> tuple[EventRecord, ...]:
    columns = (
        "sequence,event_id,schema_version,event_type,occurred_at,actor,"
        "run_id,causation_id,payload_json"
    )
    if run_id is None:
        rows = connection.execute(f"SELECT {columns} FROM events ORDER BY sequence").fetchall()
    else:
        rows = connection.execute(
            f"SELECT {columns} FROM events WHERE run_id=? ORDER BY sequence", (run_id,)
        ).fetchall()
    return tuple(_event_record(row) for row in rows)


def _query_projections(connection: sqlite3.Connection) -> tuple[ProjectionRecord, ...]:
    rows = connection.execute(
        "SELECT projection_name,entity_id,version,state_json "
        "FROM projections ORDER BY projection_name,entity_id"
    ).fetchall()
    return tuple(
        ProjectionRecord(name, entity_id, version, state_json.encode())
        for name, entity_id, version, state_json in rows
    )


def _append(
    connection: sqlite3.Connection, reducer: ProjectionReducer, event: EventEnvelope
) -> EventRecord:
    document = event_as_dict(event)
    try:
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            cursor = connection.execute(
                "INSERT INTO events(event_id,schema_version,event_type,occurred_at,actor,"
                "run_id,causation_id,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                (
                    document["event_id"],
                    document["schema_version"],
                    document["event_type"],
                    document["occurred_at"],
                    document["actor"],
                    document["run_id"],
                    document["causation_id"],
                    canonical_json(document["payload"]).decode(),
                ),
            )
            sequence = cursor.lastrowid
            if sequence is None:
                raise ProjectionAuthorityError("events", "insert did not allocate a sequence")
            snapshots = _query_projections(connection)
            for change in reducer(snapshots, event):
                if change.projection_name not in PROJECTION_NAMES:
                    raise ProjectionAuthorityError(change.projection_name, "name is not registered")
                connection.execute(
                    "INSERT INTO projections(projection_name,entity_id,version,state_json) "
                    "VALUES(?,?,?,?) ON CONFLICT(projection_name,entity_id) DO UPDATE SET "
                    "version=excluded.version,state_json=excluded.state_json",
                    (
                        change.projection_name,
                        change.entity_id,
                        change.version,
                        change.state_json.decode(),
                    ),
                )
    except sqlite3.IntegrityError as error:
        if "events.event_id" in str(error):
            raise DuplicateEventError(event.event_id) from None
        raise ProjectionAuthorityError("unknown", "database constraint rejected mutation") from None
    return EventRecord(sequence=sequence, envelope=event)


def _projection_json(records: tuple[ProjectionRecord, ...]) -> bytes:
    return canonical_json(
        tuple(
            {
                "entity_id": record.entity_id,
                "projection_name": record.projection_name,
                "state_json": record.state_json.decode(),
                "version": record.version,
            }
            for record in records
        )
    )


def _logical_digest(records: tuple[ProjectionRecord, ...]) -> str:
    protected = tuple(
        record for record in records if record.projection_name not in AUDIT_PROJECTIONS
    )
    return sha256_bytes(_projection_json(protected))


class _EventStore:
    def __init__(self, database_path: Path, reducer: ProjectionReducer) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = _connect(database_path)
        self._reducer = reducer

    def close(self) -> None:
        self._connection.close()

    def append(self, event: EventEnvelope) -> EventRecord:
        return _append(self._connection, self._reducer, event)

    def query_events(self, run_id: str | None = None) -> tuple[EventRecord, ...]:
        return _query_events(self._connection, run_id)

    def query_projection(self, name: str, entity_id: str) -> ProjectionRecord | None:
        if name not in PROJECTION_NAMES:
            raise ProjectionAuthorityError(name, "name is not registered")
        row = self._connection.execute(
            "SELECT version,state_json FROM projections "
            "WHERE projection_name=? AND entity_id=?",
            (name, entity_id),
        ).fetchone()
        return None if row is None else ProjectionRecord(name, entity_id, row[0], row[1].encode())

    def projections(self) -> tuple[ProjectionRecord, ...]:
        return _query_projections(self._connection)

    def canonical_projection_json(self) -> bytes:
        return _projection_json(self.projections())

    def logical_digest(self) -> str:
        return _logical_digest(self.projections())

    def health(self) -> StoreHealth:
        journal = self._connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = self._connection.execute("PRAGMA foreign_keys").fetchone()[0]
        synchronous = self._connection.execute("PRAGMA synchronous").fetchone()[0]
        timeout = self._connection.execute("PRAGMA busy_timeout").fetchone()[0]
        names = {0: "off", 1: "normal", 2: "full", 3: "extra"}
        return StoreHealth(journal, bool(foreign_keys), names[synchronous], timeout, 1)

    def replay_verify(self) -> ReplayVerification:
        expected_json = self.canonical_projection_json()
        expected_digest = self.logical_digest()
        with (
            TemporaryDirectory(prefix="aizim-replay-") as directory,
            closing(_connect(Path(directory) / "replay.sqlite3")) as replay,
        ):
            for record in self.query_events():
                _append(replay, self._reducer, record.envelope)
            actual_records = _query_projections(replay)
            actual_json = _projection_json(actual_records)
            actual_digest = _logical_digest(actual_records)
        return ReplayVerification(
            matched=actual_json == expected_json and actual_digest == expected_digest,
            projection_json=actual_json,
            logical_digest=actual_digest,
        )
