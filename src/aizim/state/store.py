from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from aizim.domain.serialization import canonical_json

from .capabilities import CapabilityRecord, CapabilityRow, capability_from_row
from .document_operations import DocumentOperation
from .events import EVENT_SCHEMA_VERSION, EventEnvelope, IncompatibleEventSchemaError
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
from .store_documents import execute_document_operation
from .store_mutations import (
    EventMutation,
    apply_event_mutation,
    insert_capability,
    revoke_capability,
    revoke_lease_capabilities,
)
from .store_publication_methods import PublicationStoreMethods

_SCHEMA_PATH: Final = Path(__file__).with_name("sql") / "001_foundation.sql"


def _database_guard(database_path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(database_path, flags, 0o600)
    except FileExistsError:
        descriptor = os.open(database_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise OSError("state database is not a private regular file")
        os.fchmod(descriptor, 0o600)
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _guard_matches(database_path: Path, descriptor: int) -> bool:
    try:
        current = database_path.lstat()
    except OSError:
        return False
    guarded = os.fstat(descriptor)
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_nlink == 1
        and (current.st_dev, current.st_ino) == (guarded.st_dev, guarded.st_ino)
    )


def _connect(
    database_path: Path,
    before_initialization_commit: InitializationCheckpoint = continue_initialization,
) -> sqlite3.Connection:
    guard = _database_guard(database_path)
    try:
        connection = sqlite3.connect(
            database_path.as_uri() + "?mode=rw", isolation_level=None, uri=True
        )
        initialized = False
        try:
            if not _guard_matches(database_path, guard):
                raise OSError("state database changed while opening")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
            _initialize(connection, before_initialization_commit)
            if not _guard_matches(database_path, guard):
                raise OSError("state database changed during initialization")
            initialized = True
            return connection
        finally:
            if not initialized:
                connection.close()
    finally:
        os.close(guard)


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
    columns = "sequence,event_id,schema_version,event_type,occurred_at,actor,"
    columns += "run_id,causation_id,payload_json"
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
    try:
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            snapshots = _query_projections(connection)
            record = apply_event_mutation(connection, EventMutation(reducer, event, snapshots))
            revoke_lease_capabilities(connection, event)
    except sqlite3.IntegrityError as error:
        if "events.event_id" in str(error):
            raise DuplicateEventError(event.event_id) from None
        raise ProjectionAuthorityError("unknown", "database constraint rejected mutation") from None
    return record


class _EventStore(PublicationStoreMethods):
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

    def checkpoint(self) -> None:
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

    def append(self, event: EventEnvelope) -> EventRecord:
        return _append(self._connection, self._reducer, event)

    def document[T](self, operation: DocumentOperation[T]) -> T:
        return execute_document_operation(self._connection, self._reducer, operation)

    def persist_capability(self, capability: CapabilityRecord, event: EventEnvelope) -> EventRecord:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            with self._connection:
                insert_capability(self._connection, capability)
                snapshots = _query_projections(self._connection)
                return apply_event_mutation(
                    self._connection, EventMutation(self._reducer, event, snapshots)
                )
        except sqlite3.IntegrityError as error:
            if "events.event_id" in str(error):
                raise DuplicateEventError(event.event_id) from None
            raise ProjectionAuthorityError(
                "capability_tokens", "database constraint rejected mutation"
            ) from None

    def capability_record(self, token_hash: str) -> CapabilityRecord | None:
        statement = "SELECT token_hash,run_id,worker_id,role,lease_id,operations_json,"
        statement += "expires_at,revoked_at FROM capability_tokens WHERE token_hash=?"
        row: CapabilityRow | None = self._connection.execute(statement, (token_hash,)).fetchone()
        return None if row is None else capability_from_row(row)

    def revoke_capability(self, token_hash: str, revoked_at: datetime) -> bool:
        self._connection.execute("BEGIN IMMEDIATE")
        with self._connection:
            return revoke_capability(self._connection, token_hash, revoked_at)

    def query_events(self, run_id: str | None = None) -> tuple[EventRecord, ...]:
        return _query_events(self._connection, run_id)

    def query_projection(self, name: str, entity_id: str) -> ProjectionRecord | None:
        if name not in PROJECTION_NAMES:
            raise ProjectionAuthorityError(name, "name is not registered")
        row = self._connection.execute(
            "SELECT version,state_json FROM projections WHERE projection_name=? AND entity_id=?",
            (name, entity_id),
        ).fetchone()
        return None if row is None else ProjectionRecord(name, entity_id, row[0], row[1].encode())

    def projections(self, name: str | None = None) -> tuple[ProjectionRecord, ...]:
        if name is not None and name not in PROJECTION_NAMES:
            raise ProjectionAuthorityError(name, "name is not registered")
        records = _query_projections(self._connection)
        return (
            records
            if name is None
            else tuple(record for record in records if record.projection_name == name)
        )

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
