from __future__ import annotations

from typing import Protocol


class SchemaCursor(Protocol):
    def fetchone(self) -> tuple[object, ...] | None: ...


class SchemaConnection(Protocol):
    def execute(self, sql: str, parameters: tuple[object, ...] = (), /) -> SchemaCursor: ...


def future_schema(connection: SchemaConnection, version: int) -> int | None:
    cursor = connection.execute(
        "SELECT MAX(schema_version) FROM events WHERE schema_version > ?", (version,)
    )
    row = cursor.fetchone()
    if row is None or len(row) != 1 or (row[0] is not None and type(row[0]) is not int):
        raise RuntimeError("INVALID_SCHEMA_METADATA")
    return row[0]


def ensure_artifact_associations(connection: SchemaConnection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS artifact_associations("
        "run_id TEXT NOT NULL,artifact_name TEXT NOT NULL,relative_path TEXT NOT NULL,"
        "content_hash TEXT NOT NULL,media_type TEXT NOT NULL,byte_length INTEGER NOT NULL "
        "CHECK(byte_length>=0),PRIMARY KEY(run_id,relative_path),UNIQUE(run_id,artifact_name),"
        "FOREIGN KEY(content_hash) REFERENCES artifacts(content_hash))"
    )
