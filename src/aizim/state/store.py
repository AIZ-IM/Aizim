from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from aizim.domain.serialization import canonical_json

from .events import (
    EVENT_SCHEMA_VERSION,
    EventEnvelope,
    IncompatibleEventSchemaError,
    event_as_dict,
)
from .projections import PROJECTION_NAMES, ProjectionRecord, ProjectionReducer
from .store_contracts import (
    DuplicateEventError,
    EventRecord,
    InitializationCheckpoint,
    ProjectionAuthorityError,
    ReplayVerification,
    StoreHealth,
    _event_record,
    _logical_digest,
    _projection_json,
    _projection_records,
    continue_initialization,
)

_SCHEMA_PATH: Final = Path(__file__).with_name("sql") / "001_foundation.sql"


def _connect(
    database_path: Path,
    before_initialization_commit: InitializationCheckpoint = continue_initialization,
) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialized = False
    try:
        _initialize(connection, before_initialization_commit)
        initialized = True
        return connection
    finally:
        if not initialized:
            connection.close()


def _initialize(
    connection: sqlite3.Connection,
    before_initialization_commit: InitializationCheckpoint,
) -> None:
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if user_version == 0:
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            for statement in _SCHEMA_PATH.read_text().split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(f"PRAGMA user_version={EVENT_SCHEMA_VERSION}")
            connection.execute(
                "INSERT INTO metadata(key,value_json) VALUES(?,?)",
                ("event_schema_version", str(EVENT_SCHEMA_VERSION)),
            )
            connection.execute(
                "INSERT INTO projections(projection_name,entity_id,version,state_json) "
                "VALUES(?,?,?,?)",
                ("epochs", "global", 0, canonical_json({"knowledge_epoch": 0}).decode()),
            )
            before_initialization_commit()
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
    return _projection_records(rows)


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


class _EventStore:
    def __init__(
        self,
        database_path: Path,
        reducer: ProjectionReducer,
        before_initialization_commit: InitializationCheckpoint,
    ) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = _connect(database_path, before_initialization_commit)
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
