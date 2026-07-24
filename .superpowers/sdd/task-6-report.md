# Task 6 fix/review cycle: preflight before controller claim

Date: 2026-07-24
Review finding: `start_controller` persisted `ControllerStarted` before controller or worker preflight completed.

- RED: strengthened `tests/integration/test_controller_supervisor.py::test_startup_guard_happens_before_claim` to require absent `controller_runtime/primary` and no `ControllerStarted`; the provider and stale worker-preflight cases both failed with the prior ordering (`2 failed in 0.39s`). Captured at `.omo/evidence/task-6-preflight-fix-red.txt` (its wrapper footer is not used as the pytest result).
- GREEN: moved controller preflight, worker preflight, and governor validation before `start_controller`, while retaining stale-config validation inside `start_controller` and existing cleanup/stop semantics. Focused startup guards: `19 passed in 0.54s`; Task 6 gate: `86 passed in 37.62s`; Task 5 regressions: `54 passed in 22.54s`.
- Gates: architecture `12 passed in 0.82s`; no-excuse checker clean with pure LOC `controller_supervisor.py=249`, `test_controller_supervisor.py=250`; Ruff format/check and `ty` passed; `git diff --check` passed; full suite `783 passed, 3 skipped in 181.05s`. Artifacts are under `.omo/evidence/task-6-preflight-fix-*.txt`.

# Task 6: Codex controller backend

Date: 2026-07-24
Commit subject: `Connect the Codex controller backend`
Review base: `96abd27c89934cda226dac503f78f296432471dc`

## Result

- Production controller supervision now supports only the configured Codex
  provider, uses `config.model` only for workers, and performs controller and
  worker preflight before dispatch.
- The controller pins canonical `codex-cli 0.145.0` identity and image hash,
  uses an empty private workspace, denies the canonical project, passes the
  canonical controller context only on stdin, and accepts only a bounded
  schema-validated final decision.
- The exact inner command retains
  `-c shell_environment_policy.inherit="none"`, strict config, ignored user
  config/rules, ephemeral state, read-only sandboxing, absolute schema/final
  paths, private `-C`, optional model, and stdin input.
- Synchronous version discovery is safe inside a running event loop. Both sync
  and async launch paths enforce combined output limits, bounded timeouts, and
  process-group cleanup; cancellation promptly kills and reaps the provider.
- The default-off `FILTERED_PARENT` sandbox mode validates an exact provider
  environment allowlist. Its constant outer profile selector contains no
  values. Approved values exist only in repr-hidden `parent_env`/process env;
  ordinary worker/probe requests retain `inherit="none"`.
- macOS and Linux profile compilers implement the same value-free selector and
  validation contract. The real macOS attack probe still denies secret
  environment and all protected authority surfaces.

## Authorized scope amendments

- `src/aizim/agents/launcher.py` was authorized as the existing trusted process
  owner so controller process creation did not move into orchestration.
- `src/aizim/agents/sync_process_io.py` was authorized for typed nonblocking
  sync exchange/reaping helpers only; it creates no process.
- `sandbox.py`, `permission_profile.py`, `macos_profile.py`,
  `linux_profile.py`, and their smallest existing profile tests were authorized
  after real-sandbox QA proved approved controller auth was stripped.

## TDD evidence

- Active-loop RED:
  `uv run pytest -q tests/unit/test_codex_controller.py::test_constructor_discovers_version_inside_running_event_loop`
  failed at `asyncio.run()` with
  `RuntimeError: asyncio.run() cannot be called from a running event loop`.
- Sync/cancellation GREEN:
  the constructor, EOF-input, combined-cap, cancellation/PID, and existing
  launcher lifecycle scenarios reported `18 passed in 4.38s`. Cancellation of
  a 60-second provider completed within one second and `os.kill(pid, 0)` raised
  `ProcessLookupError`.
- Actual sandbox auth RED:
  installed outer `codex-cli 0.145.0` plus a standalone fake inner provider
  exited 0 and hid the project/secrets, but observed
  `auth_present=false, HOME=null`.
- Provider environment GREEN:
  macOS/Linux profile and provider security tests reported
  `37 passed in 0.68s`; extra `NPM_TOKEN`/`DATABASE_URL` keys fail with the
  fixed `invalid provider environment` error, while secret values are absent
  from argv, profile text, policy hash, request/spec repr, and error text.

## Verification evidence

- Controller/provider/supervisor invocation:
  `uv run pytest -q tests/unit/test_codex_controller.py
  tests/security/test_controller_provider_isolation.py
  tests/security/test_agent_launcher_lifecycle.py
  tests/integration/test_controller_supervisor.py
  tests/integration/test_controller_supervisor_guards.py
  tests/unit/test_controller_decision.py
  tests/unit/test_controller_execution_transitions.py
  tests/unit/test_controller_execution_replay.py
  tests/security/test_authority_gate.py`
  reported `86 passed in 26.86s`.
- Real attack invocation:
  `uv run pytest -q tests/security/test_macos_sandbox.py`
  reported `1 passed in 0.32s`.
- Task 5 regression invocation:
  `uv run pytest -q tests/integration/test_cli_control.py
  tests/unit/test_controller_execution_transitions.py
  tests/unit/test_controller_execution_replay.py
  tests/integration/test_worker_host.py
  tests/integration/test_failure_events.py`
  reported `54 passed in 22.69s`.
- Architecture invocation:
  `uv run pytest -q tests/architecture` reported `12 passed in 0.85s`;
  process creation remains restricted to trusted launchers.
- The Python no-excuse checker reported
  `no violations in 14 file(s)`.
- Ruff format reported `15 files already formatted`; Ruff check and
  `uv run ty check` both reported `All checks passed!`.
- `git diff --check` produced no output.
- Settled full invocation:
  `uv run pytest -q` reported
  `783 passed, 3 skipped in 191.00s`.
- Pure LOC:
  `launcher.py=231`, `sync_process_io.py=55`, `sandbox.py=222`,
  `permission_profile.py=100`, `macos_profile.py=193`,
  `linux_profile.py=113`, `controller_process.py=110`,
  `codex_controller.py=248`, `controller_supervisor.py=249`,
  `codex_worker.py=247`, `test_codex_controller.py=243`,
  `test_controller_provider_isolation.py=134`,
  `test_sandbox_profile.py=239`, and
  `test_linux_profile_contract.py=126`.

## Manual actual-sandbox QA

Invocation:

```text
/opt/homebrew/bin/uv run --frozen python \
  .omo/evidence/task-6/manual_driver.py /private/tmp/<private>/codex
```

The driver used installed `/opt/homebrew/bin/codex` version `0.145.0` as the
outer sandbox, a standalone fake inner provider, the real profile compiler,
and the real bounded process launcher. It made no model call. The final
observable was:

```json
{"exit_code":0,"inner":{"auth_present":true,"home":"/private/tmp/aizim-task6-source-home","project_in_argv":false,"project_visible":false,"secret_present":false},"outer_version":"codex-cli 0.145.0"}
```

The driver also asserted that direct auth, NPM, database, and capability secret
values were absent from the full outer+inner argv and spec repr. Linux could
not be executed on the macOS host; its equivalent compiler/validator contract
is covered by the Linux profile tests above.

---

# Task 6 verification report

Date: 2026-07-21

## Scope

- Filtered workspace views use descriptor-relative no-follow reads, fresh
  copies, digest/race checks, read-only publication, and deterministic
  manifests.
- Codex `0.144.6` compiles one inline `aizim-worker` profile, runs the
  Python 3.9-compatible probe through the real macOS sandbox, scrubs the child
  environment, and disables TCP plus Unix sockets.
- Eleven probe results append only redacted audit events and remain outside the
  protected logical digest across restart/replay.

## Live compatibility findings

- Codex `0.144.6` enforces filesystem refusal with `deny`; the design draft's
  `none` value parsed but allowed canonical reads/writes.
- The `:minimal` base leaves `/private/tmp` metadata mutable. User-private
  Darwin temp roots denied `chmod`, file writes, and root rename while still
  permitting scratch writes and view reads. The adapter therefore rejects
  globally writable temp roots and canonical projects under `/private/tmp` or
  `/private/var/tmp`.

## Evidence

- RED: new Task 6 tests initially failed collection because `aizim.agents` did
  not exist; dedicated regressions then reproduced `none`, `/private/tmp`
  metadata, credential-copy, and `/private/var/tmp` failures.
- Focused final after review fixes: 40 passed on Python 3.14, including the real
  `macos_sandbox` test and architecture checks.
- Full strict suite at `e09bb0776c5842bb07199f5ae89811879e4dc5d7`:
  283 passed on Python 3.14.
- Portable Task 6 suite: 40 passed on Python 3.12.
- A 2 MiB noisy child hit `PROBE_OUTPUT_LIMIT`, received process-group
  termination, drained residual pipe data without aggregation, and was reaped.
- A missing `attack_probe.py` regression proved no subprocess is created.
- Manual sandbox view QA: `chmod_view`, `write_view`, and `rename_view_root`
  denied; `write_scratch` and `read_view` allowed.
- Manual timeout QA: `PROBE_TIMEOUT`; child process confirmed terminated.
- Ruff: passed. `ty`: passed. `git diff --check`: passed.
- `src/aizim/state/rpc.py` SHA-256 remained
  `f513efd625dd5abbe4f942e89024a571f5c4617fe6abcff4dbe4e51f8faebfcc`.
- Exact-SHA independent re-review: approved with zero Critical, Important, or
  Minor findings; both earlier Important findings were confirmed closed.
