from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from aizim.config import (
    CODEX_CLI_VERSION,
    LEAN_LSP_MCP_VERSION,
    LEAN_TOOLCHAIN,
    LEANCLIENT_VERSION,
    MCP_VERSION,
    MIN_FREE_DISK_BYTES,
)
from aizim.lean.document_io import DocumentIoError
from aizim.lean.source_layout import lakefile

CONFIG_TEMPLATE: Final = (
    "[run]\n"
    'participation = "autonomous"\n'
    'formal_participation = "formal_unassisted"\n'
    'lean_runtime = "shared"\n'
    "human_interventions = 0\n"
    "environment_frozen = true\n\n"
    "[resources]\n"
    "max_proof_workers = 2\n"
    "max_question_workers = 3\n"
    "scratch_slots = 2\n"
    "lsp_instances = 1\n"
    "local_loogle = false\n"
    "remote_search_max_concurrency = 1\n"
    f"min_free_disk_bytes = {MIN_FREE_DISK_BYTES}\n\n"
    "[foundation]\n"
    "schema_version = 1\n"
    'platform_adapter = "macos"\n'
    "repl_enabled = false\n"
    "remote_search_enabled = false\n"
    f'lean_toolchain = "{LEAN_TOOLCHAIN}"\n'
    f'lean_lsp_mcp_version = "{LEAN_LSP_MCP_VERSION}"\n'
    f'leanclient_version = "{LEANCLIENT_VERSION}"\n'
    f'mcp_version = "{MCP_VERSION}"\n'
    f'codex_cli_version = "{CODEX_CLI_VERSION}"\n'
).encode()
_DIRECTORY_MODE: Final = 0o700
_FILE_MODE: Final = 0o600


class LayoutError(ValueError):
    pass


def _regular(path: Path, label: str) -> None:
    try:
        status = path.lstat()
    except OSError as error:
        raise LayoutError(f"{label} is missing or inaccessible") from error
    if not stat.S_ISREG(status.st_mode):
        raise LayoutError(f"{label} must be a regular file")


def _private_directory(path: Path) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=_DIRECTORY_MODE)
        except OSError as error:
            raise LayoutError("Aizim runtime directory could not be created") from error
    else:
        if not stat.S_ISDIR(status.st_mode):
            raise LayoutError("Aizim runtime path must be a directory")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(descriptor, _DIRECTORY_MODE)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise LayoutError("Aizim runtime directory is not safe") from error


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 64 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _private_file(
    path: Path, *, repair_mode: bool, read_body: bool = True
) -> tuple[bytes, int]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise LayoutError("Aizim file is not safe") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise LayoutError("Aizim file is not a private regular file")
        if repair_mode:
            os.fchmod(descriptor, _FILE_MODE)
        body = _read_descriptor(descriptor) if read_body else b""
    finally:
        os.close(descriptor)
    return body, stat.S_IMODE(status.st_mode)


def _existing_private_directory(path: Path) -> None:
    try:
        status = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise LayoutError("Aizim runtime directory is not safe") from error
    os.close(descriptor)
    if not stat.S_ISDIR(status.st_mode) or stat.S_IMODE(status.st_mode) != _DIRECTORY_MODE:
        raise LayoutError("Aizim runtime directory has an invalid mode")


def _exact_private_file(path: Path, body: bytes) -> bool:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, _FILE_MODE)
    except FileExistsError:
        existing, mode = _private_file(path, repair_mode=False)
        if existing != body:
            raise LayoutError(
                "Aizim config conflicts with the fixed foundation profile"
            ) from None
        if mode != _FILE_MODE:
            _private_file(path, repair_mode=True, read_body=False)
        return False
    except OSError as error:
        raise LayoutError("Aizim config could not be created safely") from error
    try:
        os.fchmod(descriptor, _FILE_MODE)
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


@dataclass(frozen=True, slots=True)
class ProjectLayout:
    root: Path

    @classmethod
    def from_lean_project(cls, path: Path) -> ProjectLayout:
        try:
            root = path.expanduser().resolve(strict=True)
        except OSError as error:
            raise LayoutError("Lean project path does not exist") from error
        if not root.is_dir():
            raise LayoutError("Lean project path must be a directory")
        _regular(root / "lean-toolchain", "lean-toolchain")
        try:
            _regular(lakefile(root), "Lake project configuration")
        except DocumentIoError as error:
            raise LayoutError("Lake project configuration is unavailable") from error
        return cls(root)

    @property
    def state_root(self) -> Path:
        return self.root / ".aizim"

    @property
    def config_path(self) -> Path:
        return self.state_root / "config.toml"

    @property
    def database_path(self) -> Path:
        return self.state_root / "state.sqlite3"

    @property
    def run_root(self) -> Path:
        return self.state_root / "run"

    @property
    def artifact_root(self) -> Path:
        return self.state_root / "artifacts"

    @property
    def promotion_root(self) -> Path:
        return self.state_root / "promoted"

    @property
    def document_root(self) -> Path:
        return self.state_root / "documents"

    def prepare_runtime(self) -> bool:
        for directory in (
            self.state_root,
            self.run_root,
            self.artifact_root,
            self.promotion_root,
            self.document_root,
        ):
            _private_directory(directory)
        return _exact_private_file(self.config_path, CONFIG_TEMPLATE)

    def validate_database_entry(self) -> None:
        if not self.database_path.exists() and not self.database_path.is_symlink():
            return
        _private_file(self.database_path, repair_mode=False, read_body=False)

    def validate_state_lock_entry(self) -> None:
        lock_path = self.run_root / "state.lock"
        if not lock_path.exists() and not lock_path.is_symlink():
            return
        _private_file(lock_path, repair_mode=False, read_body=False)

    def validate_runtime(self) -> None:
        for directory in (
            self.state_root,
            self.run_root,
            self.artifact_root,
            self.promotion_root,
            self.document_root,
        ):
            _existing_private_directory(directory)
        config, config_mode = _private_file(self.config_path, repair_mode=False)
        if config != CONFIG_TEMPLATE or config_mode != _FILE_MODE:
            raise LayoutError("Aizim config does not match the fixed private profile")
        self.validate_state_lock_entry()
        self.validate_database_entry()
        _database, database_mode = _private_file(
            self.database_path, repair_mode=False, read_body=False
        )
        if database_mode != _FILE_MODE:
            raise LayoutError("state database has an invalid mode")

    def secure_database_permissions(self) -> None:
        try:
            descriptor = os.open(self.database_path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    raise LayoutError("state database must be a private regular file")
                os.fchmod(descriptor, _FILE_MODE)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise LayoutError("state database is not a safe regular file") from error
