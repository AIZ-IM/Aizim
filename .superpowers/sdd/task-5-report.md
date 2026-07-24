# Task 5 second re-review: authoritative project identity

Date: 2026-07-24
Commit subject: `Initialize controller project identity`
Review base: `6e90a77fdd94e70d72d32f40322b45d0ed38f847`

## Findings resolved

- Important: public `aizim init` now creates the sole durable
  `ProjectInitialized` event. Re-init validates but never duplicates or rewrites
  it; missing identity is created, while conflicting, multiple, or structurally
  invalid existing identities fail closed.
- The created smoke-project identity uses `project.name`,
  `smoke_base_epoch(project)`, and initial `knowledge_epoch = 0`. Marker-only
  Lean projects retain the pre-existing public-init contract through a
  deterministic hash of their validated `lakefile.toml` and `lean-toolchain`.
- Existing identities validate their immutable initial hash structurally
  instead of comparing it to the current global epoch, so legitimate
  `KnowledgeDeltaPublished` evolution does not make re-init fail.
- Minor: the recording worker backend now checks at worker entry that the exact
  directive-artifact directory is non-empty and the durable worker-execution
  projection is already `planned`. The former post-hoc-only ordering assertion
  was removed.
- Happy-path supervisor and CLI-controller fixtures no longer append
  `ProjectInitialized`; they prove the public init boundary supplies identity.
  The guard fixture alone bypasses or duplicates initialization to test
  fail-closed states.

## Second scope amendment

The parent explicitly authorized this re-review to modify
`src/aizim/cli/init_command.py` and `tests/integration/test_cli_init.py` in
addition to the existing Task 5 controller files and this report. Public init
is the authoritative project-identity boundary. No other files were modified.

## TDD evidence

- Public-init RED:
  `uv run pytest -q tests/integration/test_cli_init.py -k 'creates_only_private or invalid_existing_identity'`
  reported `4 failed, 9 deselected`. The happy path observed zero
  `ProjectInitialized` events (`0 == 1`), while conflict, multiple, and
  advanced-initial-epoch cases returned exit 0 instead of exit 2.
- Public-init GREEN:
  the identity/idempotence/conflict/evolution subset reported
  `5 passed, 9 deselected in 2.90s`.
- Settled focused init/supervisor/guard/CLI plus Task 2/4 execution, replay,
  host, and failure regressions reported `92 passed in 30.06s`.

## Static and full-suite evidence

- Ruff check and format check passed for all five changed Python files.
- `uv run ty check` reported `All checks passed!`.
- Authority/trusted-boundary tests reported `12 passed in 1.10s`.
- The Python no-excuse checker reported `no violations in 5 file(s)`.
- Pure LOC:
  `init_command.py=67`,
  `controller_dispatcher.py=244`,
  `controller_supervisor.py=248`,
  `test_cli_init.py=240`,
  `test_controller_supervisor.py=250`,
  `test_controller_supervisor_guards.py=146`, and
  `test_cli_controller_start.py=185`.
- `git diff --check` passed.
- The single settled `uv run pytest -q` reported
  `768 passed, 3 skipped in 159.86s`.

## Manual public CLI evidence

The manual composition used public init twice, public configure/register,
post-start public assignment, foreground signal handling, and foreground
restart. No direct identity event was inserted:

```text
MANUAL PUBLIC INIT CONTROLLER PASS
project_initialized=1 reinit_duplicates=0 live_assignment=1
artifact_before_worker=1 planned_before_worker=1 worker_model=worker-model
active_signal=interrupted restart_duplicates=0 leases=0 sockets=0 pid=0
```

---

# Task 5 controller supervisor review hardening

Date: 2026-07-24
Commit subject: `Harden controller supervisor ordering`
Review base: `33de7198ca195e6b1369c3b328316fd512dc3302`

## Result

- The production default worker preflight now fails closed with the fixed
  `WORKER_PREFLIGHT_UNAVAILABLE` code before any assignment claim.
- Every scan validates exactly one durable project identity, snapshots the
  durable base/knowledge epochs, and validates both trusted execution and
  directive IDs before claim. The carried snapshot and IDs are the values used
  for planning and dispatch.
- Direct `DispatchDecision` values are checked against instruction, budget, and
  timeout ceilings before artifact creation or worker launch.
- The supervisor acknowledges entry into the cancellable dispatch task before
  publishing it as active, closing cancel-before-first-task-step handoff.
- Nested, cancellation-resistant finalization attempts stop recording,
  checkpoint, state close, and PID/socket ownership close. Cleanup failure
  cannot replace an already active primary failure.
- Tests now force a genuine idle wake from a post-start public assignment,
  observe directive-artifact/planned-event ordering, verify the worker-model
  source, cancel a genuinely active worker, and cover identity/epoch/ID,
  direct-ceiling, and finalization failures.

## Review-fix scope amendment

The parent explicitly authorized one narrow, non-duplicative regression module,
`tests/integration/test_controller_supervisor_guards.py`, after the complete
guard matrix could not remain within the 250-pure-line contract in the original
CLI test. The original module retains CLI wiring, active-worker cancellation,
and restart recovery; the new module owns only startup identity/epoch/ID,
default-preflight, and finalization guard coverage. No other scope expanded.

## TDD evidence

- Initial guard RED:
  `uv run pytest -q tests/integration/test_cli_controller_start.py -k fail_closed_startup_guards_precede_claim`
  reported `2 failed`; missing project identity and the production-default
  worker preflight both failed with `DID NOT RAISE`.
- Finalization RED:
  `uv run pytest -q tests/integration/test_cli_controller_start.py -k 'fail_closed or finalization or stop_interrupts'`
  reported `1 failed, 9 passed, 5 deselected`; annotating a frozen
  `ControllerExecutionError` with `add_note()` raised `FrozenInstanceError` and
  replaced the primary failure.
- Focused GREEN:
  `uv run pytest -q tests/integration/test_controller_supervisor.py tests/integration/test_controller_supervisor_guards.py tests/integration/test_cli_controller_start.py`
  reported `24 passed in 0.53s`.
- Task 2 execution/lifecycle plus Task 4 host/failure GREEN:
  `uv run pytest -q tests/integration/test_cli_control.py tests/unit/test_controller_execution_transitions.py tests/unit/test_controller_execution_replay.py tests/integration/test_worker_host.py tests/integration/test_failure_events.py`
  reported `54 passed in 18.98s`.

## Static and regression evidence

- Ruff check passed and Ruff format reported all five changed Python files
  formatted.
- `uv run ty check` reported `All checks passed!`.
- Authority/trusted-boundary tests reported `12 passed in 0.72s`.
- The Python no-excuse checker reported `no violations in 5 file(s)`.
- Pure LOC:
  `controller_dispatcher.py=244`,
  `controller_supervisor.py=248`,
  `test_controller_supervisor.py=250`,
  `test_controller_supervisor_guards.py=142`, and
  `test_cli_controller_start.py=202`.
- `git diff --check` passed.
- `uv run pytest -q` reported `764 passed, 3 skipped in 148.41s`.

## Manual CLI composition evidence

A temporary Lean fixture was initialized and configured without an assignment.
The foreground CLI controller was then started, a deliberate live assignment
was submitted through the public worker CLI, and `SIGTERM` was delivered only
after the worker backend entered. The same foreground CLI boundary was
restarted, and durable state/artifact ordering plus residue were inspected:

```text
MANUAL CONTROLLER HARDENING PASS
live_assignment=1 planned_before_worker=1 worker_model=worker-model
active_signal=interrupted restart_duplicates=0 leases=0 sockets=0 pid=0
```

---

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
