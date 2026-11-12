# Foreground controller execution-loop final gate review

## recommendation

**APPROVE**

Reviewed exact SHA: `87640901c7448206b6363e9d39e095e70b6c8c70`

Merge-base: `c8277a2eab483d42ba8795169288e56d945bbb7a`

Review package: `.superpowers/sdd/review-c8277a2..8764090.diff`

## blockers

None.

## originalIntent

Deliver the complete Tasks 1-9 foreground controller execution loop: a single
live state writer with narrow live mutation RPC; durable controller lifecycle,
claim, dispatch, terminal, replay, restart, and cancellation semantics; bounded
and provider-neutral controller decisions; provider isolation; Codex-only
workers; exact package-local provider versions; safe status projection; and
source/installed distribution proof. `allowed_operations` are model-visible
descriptive labels only and never confer authority.

## desiredOutcome

At the reviewed SHA, an initialized Aizim project can run
`aizim controller start --project PROJECT --foreground`, accept live supported
control mutations, execute an assignment exactly once through the existing
Codex worker authority boundary, survive restart without replaying terminal
work, and expose a redacted status projection. Both controller providers use
the same canonical context and decision contract while remaining isolated from
project state and unrelated credentials. The npm package resolves exact local
Codex `0.145.0` and Claude `2.1.218` executables on the three supported targets.

## userOutcomeReview

The shipped artifact satisfies the requested outcome at the exact reviewed SHA.
The live supervisor owns `StateService`, performs controller/worker/resource
preflight before `start_controller` and before the loop can claim work, then
uses durable projections to skip existing executions. Dispatch authority is
derived independently from the claimed worker role; controller-visible
`allowed_operations` are only validated serialization labels. Worker execution
uses the existing lease/capability/gateway/Lean host and closes it in `finally`.
Cancellation records interruption and propagates while finalization stops the
controller, checkpoints/closes state, and releases ownership.

Codex and Claude both feed `controller_context_bytes(context)` to the provider
and parse into the shared decision union. Their argv and environments are
provider-specific, exact-version pinned, private-workspace based, OS-sandboxed,
and filtered. Workers remain constructed with `CodexWorkspaceBackend`.

The deterministic source smoke was independently reproduced at this SHA:
one execution and zero restart duplicates. The installed-wheel equivalent,
full build/test/check/pack, and fresh-consumer checks are accepted from
SHA-bound artifacts. Credentialed real-provider QA is honestly
`SKIPPED_AUTH_UNAVAILABLE`; it is not counted as a pass.

## final checklist

| Criterion | Result | Evidence |
| --- | --- | --- |
| FC-01 tracked worktree clean | SATISFIED | `git status --short` reports only untracked `.omo/`; tracked and staged diffs are empty in `12-final-audit.txt`. |
| FC-02 one live writer and live control mutations | SATISFIED | `controller_supervisor.py:72-85`; focused state/control/supervisor tests in the independent 132-test run. |
| FC-03 byte-identical bounded provider context and shared decision union | SATISFIED | Both provider implementations call `controller_context_bytes`; Task 8 review and provider-isolation tests; independent 132-test run. |
| FC-04 readiness before claim | SATISFIED | `controller_supervisor.py:100-124`, with claim only later in `_loop`/`prepare_next`; guard tests reproduced. |
| FC-05 exactly one terminal execution | SATISFIED | transition/replay suites reproduced; source smoke reports `assignment_executions=1`. |
| FC-06 terminal assignment not rerun after restart | SATISFIED | `controller_dispatcher.py:196-202`; replay/supervisor suites reproduced; smoke reports `restart_duplicates=0`. |
| FC-07 nonterminal restart interruption/new version | SATISFIED | startup recovery at `controller_supervisor.py:87-89`; replay/transition tests reproduced. |
| FC-08 signal cleanup/revocation/lease/interruption/checkpoint/exit | SATISFIED | supervisor cancellation/finalization and worker-host cleanup paths; supervisor and worker-host suites reproduced. |
| FC-09 controller isolation | SATISFIED | locked provider argv/environment/private workspace; security/provider-isolation and authority tests reproduced. |
| FC-10 exact package-local providers on three targets | SATISFIED | `lib/platform.mjs` has exactly darwin-arm64/linux-arm64/linux-x64 aliases; exact pins in package/build/launcher; fresh-consumer artifact shows no global fallback. Cross-target resolution is accepted from deterministic package tests and manifests rather than hardware execution. |
| FC-11 build/test/check/pack | SATISFIED | final-SHA artifacts `05`-`08`, SHA audit in `12-final-audit.txt`; aggregate reports 91 Node, 23 Rust, and 805 Python passing. |
| FC-12 source and installed smoke | SATISFIED | independent source smoke plus `06-npm-test.txt`; each reports one execution and zero duplicates. |
| FC-13 credentialed provider QA | HONESTLY SKIPPED | `09-manual-controller.txt`: two `AUTH_UNAVAILABLE` skips, explicitly not passes. This satisfies the checklist's allowed skip branch. |

Additional required invariants:

- Status projection secrecy: satisfied by the projection allowlist and malformed/
  secret-input tests in the independent run; no prompt, provider envelope,
  capability, token, or authority field is emitted.
- Descriptive-label-only boundary: satisfied. Context serialization validates
  labels, while dispatcher authority is recomputed from `controller_worker_tools(claim.role)`.
- Codex-only workers: satisfied by dispatcher construction of
  `CodexWorkspaceBackend`; Claude is selected only through the controller factory.
- Pure LOC: satisfied. Independent measurement covered all 67 changed Python
  files and found zero files above 250 pure LOC.
- Task 9 ten-file closure: satisfied by `00-no-excuse-green.txt`,
  `01-static-green.txt`, `02-focused-60-green.txt`, `03-pure-loc.txt`, and the
  final exact-SHA package evidence.

## remove-ai-slops and programming direct pass

The cumulative diff, production boundaries, and tests were reviewed directly,
in addition to confirming that the Task 8 and Task 9 code-review reports
explicitly cover these perspectives.

No excessive/removal-only/deletion-only tests, tautological assertions,
implementation-derived expectations, prompt-wording pins, or unnecessary
production extraction/parsing/normalization were found that violate a success
criterion. The tests assert observable state transitions, provider bytes,
redacted projections, package resolution, or public CLI output. Production
parsing is located at untrusted JSON/RPC boundaries. Broad catches are confined
to marked lifecycle/worker/top-level redaction boundaries. Exhaustive decision
matching uses `match` plus `assert_never`. No changed Python file exceeds the
250-pure-LOC ceiling.

The exact Task 8 and Task 9 review reports both explicitly record the same
overfit/slop criteria and programming checks; report coverage agrees with, but
does not replace, this direct pass.

## commands and results

- `git rev-parse HEAD` -> exact reviewed SHA.
- `git merge-base origin/main HEAD` -> exact supplied base.
- `git diff --check BASE..SHA` -> exit 0.
- Changed-Python pure-LOC audit -> 67 files checked; zero over 250.
- Invariant-focused pytest command covering decision, transitions, replay,
  Codex, Claude, live RPC, supervisor guards, worker host, projection,
  provider isolation, and authority -> `132 passed in 5.41s`.
- `uv run --frozen python scripts/qa/controller_smoke.py` ->
  `CONTROLLER SMOKE PASS`, `assignment_executions=1`,
  `restart_duplicates=0`.
- Post-check `git status --short` -> only `?? .omo/`.

## checked artifact paths

- `docs/plans/2026-07-24-controller-execution-loop.md`
- `.superpowers/sdd/review-c8277a2..8764090.diff`
- `.superpowers/sdd/task-1-report.md` through `task-9-report.md`, including
  referenced briefs/amendments
- `.omo/evidence/review-ledger.md`
- `.omo/evidence/task8-claude-controller-code-review.md`
- `.omo/evidence/task8-postcommit-verification/*`
- `.omo/evidence/task9-controller-operation-code-review.md`
- `.omo/evidence/task9-controller-operation/stop-hook-1/EVIDENCE.md`
- `.omo/evidence/task9-controller-operation-review-fix/EVIDENCE.md`
- `.omo/evidence/task9-controller-operation-review-fix/00-no-excuse-green.txt`
  through `12-final-audit.txt`
- Current cumulative production and test source in the supplied 26-commit range

## evidence pairs accepted/rejected

Accepted:

- Exact-SHA build/test/check/pack artifacts paired with `12-final-audit.txt`.
- Source smoke independently reproduced, paired with installed smoke in
  `06-npm-test.txt`.
- Fresh consumer `10-fresh-consumer.txt`, paired with package manifests and
  exact pinned platform table.
- Task 8 approval at `5f382672...`, paired with exact-SHA Task 9 approval at
  `87640901...`.
- Credential absence paired with literal `AUTH_UNAVAILABLE` skips.

Rejected as pass evidence:

- Earlier Task 8 prose that implied host credential readiness. The exact-SHA
  Task 8 review correctly rejects that interpretation.
- HTTP/process exit status alone without payload assertions.
- The older Task 9 approval target `1221ddc...` as final-SHA evidence; only the
  `87640901...` correction evidence and re-review bind the final verdict.

## exact evidence gaps

No gap violates a stated success criterion.

The three supported package targets were not each executed on native hardware
in this review environment; deterministic target manifest/resolution tests,
package contents, and the host fresh-consumer check are the available evidence.
The criterion asks that package-local resolution be supported on all three
targets, which those artifacts establish.

Credentialed controller execution remains unavailable and is explicitly
recorded as `SKIPPED_AUTH_UNAVAILABLE`, as permitted by FC-13.

## residual risks

- CPython 3.14 frozen dataclass exceptions can mask a primary re-raise with
  `FrozenInstanceError: cannot assign to field '__traceback__'`. The independent
  reproduction is in
  `.omo/evidence/task9-controller-operation/45-frozen-exception-repro.txt`.
  This is pre-existing/cross-cutting and separately scoped. The shipped
  controller criteria are exercised green without relying on the masked path,
  so it is a non-blocking residual risk, not a failed checklist item.
- Several files sit exactly at or near the 250-pure-LOC ceiling. That is future
  maintenance pressure, not a present criterion failure.
