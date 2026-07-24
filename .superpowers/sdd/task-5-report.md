# Task 5 controller supervisor verification report

Date: 2026-07-24
Commit subject: `Run the foreground controller loop`

## Result

- Added the foreground `ControllerSupervisor`, trusted dispatcher, and
  `controller start --project PROJECT --foreground` CLI boundary.
- The loop owns the sole live state service, recovers unclean controller and
  planned execution state, wakes on live control RPC mutations, dispatches in
  stable worker order, and records exactly one terminal outcome.
- Controller, worker, resource, model, and trusted-ID readiness failures occur
  before claim. Blocked, rejected, malformed, timed-out, worker-failed, and
  cancelled work use the required fixed terminal reason codes.
- Signal registration is confined to the top-level CLI adapter. The command
  rejects omitted `--foreground` and exposes neither detached mode nor stop.

## TDD evidence

- Supervisor RED:
  `/opt/homebrew/bin/uv run --frozen pytest tests/integration/test_controller_supervisor.py -q`
  failed during collection with
  `ModuleNotFoundError: No module named 'aizim.orchestration.controller_supervisor'`.
- CLI RED:
  `uv run pytest -q tests/integration/test_cli_controller_start.py`
  failed during collection with
  `ModuleNotFoundError: No module named 'aizim.cli.controller_command'`.
- GREEN focused gate:
  `/opt/homebrew/bin/uv run --frozen pytest tests/integration/test_controller_supervisor.py tests/integration/test_cli_controller_start.py tests/integration/test_cli_control.py -q`
  reported `41 passed in 20.80s`.
- The focused matrix covers restart idempotence, live RPC wakeup, provider
  preflight, stale controller version, blocked/rejected/malformed/timed-out
  decisions, cancellation cleanup, unclean startup recovery, missing worker
  model, safe CLI errors, and foreground-only usage.

## Static and architecture evidence

- The no-excuse checker reported `no violations in 6 file(s)`.
- The exact requested Ruff check passed; Ruff format check reported
  `7 files already formatted`.
- `/opt/homebrew/bin/uv run --frozen ty check` reported `All checks passed!`.
- The authority/trusted-boundary gate reported `12 passed in 0.78s`.
- Pure LOC after formatting:
  `controller_dispatcher.py=234`,
  `controller_supervisor.py=245`,
  `controller_command.py=45`,
  `test_controller_supervisor.py=236`, and
  `test_cli_controller_start.py=204`.
- `git diff --check` passed.

## Full regression evidence

- `/opt/homebrew/bin/uv run --frozen pytest -q` reported
  `755 passed, 3 skipped in 142.06s`.

## Manual CLI composition evidence

- A temporary Lean fixture was initialized through `main()`, then configured,
  registered, and assigned through the public CLI command boundary.
- `controller start --project PROJECT --foreground` ran with deterministic
  injected controller and submitted-worker backends, then restarted through
  the same CLI boundary.
- The final observed output was:

```text
CLI CONTROLLER COMPOSITION PASS
assignment_executions=1
restart_duplicates=0
```

- Two earlier QA-driver attempts returned the intentionally scrubbed
  `aizim controller start: controller failed`. Diagnosis showed the driver
  closure emitted a `directive-*` value from the session-ID factory; the
  supervisor correctly rejected it as `TRUSTED_ID_INVALID`. Correcting the
  driver factory produced the passing evidence above without a production
  change.

## Scope and concerns

- `src/aizim/cli/control_command.py` remains semantically and textually
  unchanged; existing configure/show behavior was preserved as required.
- No provider-specific production controller backend is introduced here;
  deterministic injection is the Task 5 boundary, while provider connection is
  planned for Tasks 6 and 8.
- No push, PR, detached controller mode, or controller stop command was created.

## Superseded archive: unrelated prior Task 5 report

# Task 5 verification report

Date: 2026-07-21
Commit: `35e9aa9a66cca4f927af9e60c728db50398e1933`

## Result

- Project-owned layout and fixed private configuration implemented.
- `init`, `doctor`, `status`, and hidden `state serve` implemented.
- Durable projection enumeration added without changing RPC framing.
- PID/socket lifecycle is PID-aware, mode-safe, and inode-aware.
- Corrupt state, linked state files, conflicting config, and unsafe runtime entries fail closed.

## Automated evidence

- Strict full suite with asyncio debug and promoted unraisable warnings: `238 passed`.
- Python 3.12 focused lifecycle/config/RPC/architecture suite: `63 passed`.
- Python 3.14 focused lifecycle/config/RPC/architecture suite: `63 passed`.
- Task 3 and Task 4 regression suite: `149 passed`.
- Ruff: passed.
- `ty check`: passed.
- `git diff --check`: passed.
- Task 5 architecture and pure-LOC gate: passed; maximum authored file is 248 pure LOC.
- `src/aizim/state/rpc.py` SHA-256 remained
  `f513efd625dd5abbe4f942e89024a571f5c4617fe6abcff4dbe4e51f8faebfcc`.

## Manual CLI evidence

- Initialized the same temporary Lean project twice successfully.
- Observed `.aizim` and `.aizim/run` mode `0700`.
- Observed `config.toml` and `state.sqlite3` mode `0600`.
- `doctor` reported all 12 checks as `PASS` and ended with `READY`.
- `status --json` returned the exact eight-key document with
  `epochs.knowledge_epoch = 0`.
- Started, queried, SIGTERM-stopped, and restarted `state serve` twice.
- Both shutdowns removed the owned PID/socket and truncated the WAL sidecar.
- Public `aizim --help` does not expose the hidden state supervisor command.

## Exact-commit verification

- Re-ran the strict full suite at the exact commit: `238 passed`.
- Re-ran Ruff, full `ty check`, `git diff --check`, and the RPC hash guard.
- The tracked worktree and index were clean after verification.

## Independent review of `35e9aa9`

Verdict: changes requested, with four Important findings and no Critical
findings.

- A malicious version executable could echo secret-named environment values.
- Readiness/runtime failures did not use the global exit-code contract.
- Live state health accepted an unowned socket and did not validate schema 1.
- State lock/database creation still relied on path opens after layout checks;
  initial database/WAL/SHM modes were `0644` during initialization.

## Review fixes

- Version commands now receive an environment with `KEY`, `SECRET`, `TOKEN`,
  `PASSWORD`, and `CREDENTIAL` names removed.
- Invalid configuration remains exit 2; readiness preflight is exit 3; valid
  runtime state failures are exit 6.
- Live status/health requires a live private PID record, a mode-0600 Unix
  socket, stable ownership, schema 1, and `ready = true`.
- State lock creation/open uses `O_EXCL | O_NOFOLLOW` and descriptor checks.
- SQLite files are descriptor-guarded, pre-created mode `0600`, checked by
  inode before and after initialization, and produce mode-0600 WAL/SHM files.
- Ten focused regressions first failed and then passed.
- Post-fix strict full suite: `245 passed`.
- Post-fix Python 3.12 and 3.14 focused suites: `78 passed` each.
- Post-fix Task 3/4 regression suite: `149 passed`.
- Post-fix manual service QA observed database, WAL, SHM, PID, and socket all
  at mode `0600`; second owner exited 3; SIGTERM cleanup succeeded.

## Second independent review of `d5f4740`

Verdict: changes requested, with two remaining Important findings and no
Critical findings.

- A direct live `StateService` lock owner without PID/socket was still mapped
  to runtime exit 6 rather than preflight exit 3.
- A replacement symlink to a nonexistent external database could still cause
  `sqlite3.connect` to create the external target before inode mismatch was
  reported.

Both are now covered by deterministic regressions. Direct
`StateServiceLifecycleError` ownership conflicts map to exit 3. SQLite connects
only to the already descriptor-created file using a `mode=rw` URI, so it cannot
create a replacement symlink target. The strict full suite now reports
`247 passed`; Python 3.12 and 3.14 focused suites report `80 passed` each.

The review's scope concern is resolved by
`.superpowers/sdd/task-5-review-amendment.md`, which explicitly authorizes
`src/aizim/state/service_ownership.py` for the review fix.

## Final review

Approved at exact SHA `4b3ead79212f9ae12b40b7b331661a7d53acd248`
with 0 Critical, 0 Important, and 0 Minor findings.

Decisive final probes reported direct owner exit 3, replacement rejection, and
no external database target creation. Focused review verification reported
`43 passed`; the RPC hash and all scope/LOC guards remained intact.

## Scope

- Only paths in the approved Task 5 plan and scope amendment are tracked changes.
- `.omc/` and `docs/superpowers/plans/` remain untracked and untouched.
