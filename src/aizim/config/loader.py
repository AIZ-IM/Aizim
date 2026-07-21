from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import assert_never

from .model import (
    CODEX_CLI_VERSION,
    LEAN_LSP_MCP_VERSION,
    LEAN_TOOLCHAIN,
    LEANCLIENT_VERSION,
    MCP_VERSION,
    MIN_FREE_DISK_BYTES,
    AizimConfig,
    ConfigError,
    FormalParticipation,
    LeanRuntimeMode,
    ParticipationMode,
    ResourcePolicy,
    RunPolicy,
)

type TomlScalar = None | bool | int | float | str | datetime | date | time
type TomlValue = TomlScalar | list[TomlValue] | dict[str, TomlValue]


def _table(config: Mapping[str, TomlValue], key: str) -> dict[str, TomlValue]:
    value = config.get(key, {})
    match value:
        case dict():
            return value
        case None | bool() | int() | float() | str() | datetime() | date() | time() | list():
            raise ConfigError(key, "must be a TOML table")
        case unreachable:
            assert_never(unreachable)


def _known_keys(
    table: Mapping[str, TomlValue],
    allowed: frozenset[str],
    location: str,
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(location, f"unknown keys: {', '.join(unknown)}")


def _string(table: Mapping[str, TomlValue], key: str, default: str) -> str:
    value = table.get(key, default)
    if type(value) is not str:
        raise ConfigError(key, "must be a string")
    return value


def _integer(table: Mapping[str, TomlValue], key: str, default: int) -> int:
    value = table.get(key, default)
    if type(value) is not int:
        raise ConfigError(key, "must be an integer")
    return value


def _boolean(table: Mapping[str, TomlValue], key: str, default: bool) -> bool:
    value = table.get(key, default)
    if type(value) is not bool:
        raise ConfigError(key, "must be a boolean")
    return value


def _enum_value[E: StrEnum](value: str, enum_type: type[E], location: str) -> E:
    try:
        return enum_type(value)
    except ValueError as error:
        raise ConfigError(location, f"unknown value {value!r}") from error


def load_config(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> AizimConfig:
    path = project_root / ".aizim" / "config.toml"
    if path.is_file():
        with path.open("rb") as stream:
            raw: dict[str, TomlValue] = tomllib.load(stream)
    else:
        raw = {}
    _known_keys(raw, frozenset({"model", "run", "resources", "foundation"}), "config")
    run_table = _table(raw, "run")
    resources_table = _table(raw, "resources")
    foundation_table = _table(raw, "foundation")
    _known_keys(
        run_table,
        frozenset(
            {
                "participation",
                "formal_participation",
                "lean_runtime",
                "human_interventions",
                "environment_frozen",
            }
        ),
        "run",
    )
    _known_keys(
        resources_table,
        frozenset(
            {
                "max_proof_workers",
                "max_question_workers",
                "scratch_slots",
                "lsp_instances",
                "local_loogle",
                "remote_search_max_concurrency",
                "min_free_disk_bytes",
            }
        ),
        "resources",
    )
    _known_keys(
        foundation_table,
        frozenset(
            {
                "lean_toolchain",
                "lean_lsp_mcp_version",
                "leanclient_version",
                "mcp_version",
                "codex_cli_version",
                "schema_version",
                "platform_adapter",
                "repl_enabled",
                "remote_search_enabled",
            }
        ),
        "foundation",
    )
    participation = _enum_value(
        _string(run_table, "participation", ParticipationMode.AUTONOMOUS.value),
        ParticipationMode,
        "run.participation",
    )
    match participation:
        case ParticipationMode.AUTONOMOUS:
            formal_default, frozen_default = FormalParticipation.UNASSISTED, True
        case ParticipationMode.COLLABORATIVE:
            formal_default, frozen_default = FormalParticipation.ASSISTED, False
        case ParticipationMode.LEARNING:
            formal_default, frozen_default = FormalParticipation.EDUCATIONAL, False
        case unreachable:
            assert_never(unreachable)
    run = RunPolicy(
        participation=participation,
        formal_participation=_enum_value(
            _string(run_table, "formal_participation", formal_default.value),
            FormalParticipation,
            "run.formal_participation",
        ),
        lean_runtime=_enum_value(
            _string(run_table, "lean_runtime", LeanRuntimeMode.SHARED.value),
            LeanRuntimeMode,
            "run.lean_runtime",
        ),
        human_interventions=_integer(run_table, "human_interventions", 0),
        environment_frozen=_boolean(run_table, "environment_frozen", frozen_default),
    )
    resources = ResourcePolicy(
        max_proof_workers=_integer(resources_table, "max_proof_workers", 2),
        max_question_workers=_integer(resources_table, "max_question_workers", 3),
        scratch_slots=_integer(resources_table, "scratch_slots", 2),
        lsp_instances=_integer(resources_table, "lsp_instances", 1),
        local_loogle=_boolean(resources_table, "local_loogle", False),
        remote_search_max_concurrency=_integer(
            resources_table, "remote_search_max_concurrency", 1
        ),
        min_free_disk_bytes=_integer(
            resources_table, "min_free_disk_bytes", MIN_FREE_DISK_BYTES
        ),
    )
    fixed_foundation = (
        (_integer(foundation_table, "schema_version", 1), 1, "schema_version"),
        (_string(foundation_table, "platform_adapter", "macos"), "macos", "platform_adapter"),
        (_boolean(foundation_table, "repl_enabled", False), False, "repl_enabled"),
        (
            _boolean(foundation_table, "remote_search_enabled", False),
            False,
            "remote_search_enabled",
        ),
    )
    for actual, required, location in fixed_foundation:
        if actual != required:
            raise ConfigError(location, f"must equal fixed foundation value {required!r}")
    file_model = raw.get("model")
    if file_model is not None and type(file_model) is not str:
        raise ConfigError("model", "must be a string")
    environment = os.environ if environ is None else environ
    model = environment.get("AIZIM_MODEL", file_model)
    if model is not None and type(model) is not str:
        raise ConfigError("AIZIM_MODEL", "must be a string")
    return AizimConfig(
        run=run,
        resources=resources,
        model=model,
        lean_toolchain=_string(foundation_table, "lean_toolchain", LEAN_TOOLCHAIN),
        lean_lsp_mcp_version=_string(
            foundation_table, "lean_lsp_mcp_version", LEAN_LSP_MCP_VERSION
        ),
        leanclient_version=_string(
            foundation_table, "leanclient_version", LEANCLIENT_VERSION
        ),
        mcp_version=_string(foundation_table, "mcp_version", MCP_VERSION),
        codex_cli_version=_string(
            foundation_table, "codex_cli_version", CODEX_CLI_VERSION
        ),
    )
