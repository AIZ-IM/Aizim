# Task 7 verification report

## 2026-07-24 — Exact-SHA review fix: reject executable directories

Reviewed commit: `20e386ee7310b84c4a7f4b984c72faa44be4b4ff`

### Finding and fix

Exact-SHA review found that `realpathSync()` plus `accessSync(X_OK)` accepted a
mode-755 directory named `claude` on POSIX. The resolver then returned that
directory, and `resolveAssets()` rejected it later through an untyped
regular-file error rather than the stable package boundary.

The narrow fix requires `statSync(executable).isFile()` after canonicalization
and before the execute-access check, inside `resolveClaudeExecutable()`'s
existing `try` block. A directory, device, missing path, or access failure is
therefore normalized to `CLAUDE_PACKAGE_INVALID`; launch error reporting maps
that code to configuration exit `78`. The pre-existing Codex resolver and all
other distribution behavior remain unchanged.

### TDD evidence

| Scenario | Invocation | Binary observable | Captured artifact |
|---|---|---|---|
| Executable-directory RED | `node --test tests/npm/claude-resolution.test.mjs` after adding a mode-755 directory fixture and before the fix | exit `1`; `6` passed, `1` failed with `Missing expected exception` | `.superpowers/sdd/task-7-report.md` |
| Typed-boundary GREEN | the same focused invocation after adding the regular-file check | exit `0`; `7` passed, including `CLAUDE_PACKAGE_INVALID`, safe diagnostic text, and exit `78` for the directory | `.superpowers/sdd/task-7-report.md` |

### Verification

| Scenario | Invocation | Binary observable | Captured artifact |
|---|---|---|---|
| Full Node suite | `npm run test:node` | exit `0`; `89` passed, `0` failed | `.superpowers/sdd/task-7-report.md` |
| Build | `npm run build` | exit `0`; `Built @aiz.im/aizim 0.1.0 for darwin-arm64` | `.superpowers/sdd/task-7-report.md` |
| Freshness | `npm run check` | exit `0`; `All checks passed!`; checked `darwin-arm64` | `.superpowers/sdd/task-7-report.md` |
| Package | `npm run pack` | exit `0`; both platform and meta tarballs emitted | `.superpowers/sdd/task-7-report.md` |
| Packed-source boundary | `tar -xOf dist/npm/aiz.im-aizim-0.1.0.tgz package/lib/claude.mjs` and exact regular-file assertion | exit `0`; packed line `if (!statSync(executable).isFile()) {` observed | `.superpowers/sdd/task-7-report.md` |
| Native manual QA | `env -i PATH=/usr/bin:/bin node_modules/@anthropic-ai/claude-code-darwin-arm64/claude --version` | exit `0`; exact output `2.1.218 (Claude Code)` | `.superpowers/sdd/task-7-report.md` |
| Patch hygiene | `git diff --check` plus forbidden escape-hatch scan of both touched code/test files | exit `0`; no whitespace errors or escape hatches | `.superpowers/sdd/task-7-report.md` |

The closest affected installed-package gate was used instead of another full
local/global provisioning smoke: repeated stop-hook verification had already
driven available disk close to the real smoke's peak `disk_floor` boundary.
The previously committed full local/global smoke passed at the reviewed SHA,
and this review fix changes only the packaged resolver validation. The fresh
build/check/pack, packed-source inspection, full Node suite, and real native
binary invocation directly cover the changed surface without weakening the
product threshold or touching user caches.

### Scope

- Modified only `lib/claude.mjs`,
  `tests/npm/claude-resolution.test.mjs`, and this durable report.
- Exact commit subject: `Reject invalid Claude executable paths`.
- This commit requires a fresh exact-SHA rereview before Task 7 approval.

---

## 2026-07-24 — Carry the exact Claude executable through npm and Rust

Base HEAD: `0c32bf56225146d89f54b97003f30489891b9ab6`

### Result

- Pinned `@anthropic-ai/claude-code` to exact version `2.1.218` with
  `npm install --save-exact --ignore-scripts`. The new resolver reads the exact
  meta/native package manifests, accepts only the three Aizim release targets,
  canonicalizes and executable-checks the native root-level `claude` binary,
  and never falls back to `PATH`.
- Carried the resolved Claude executable through Node asset resolution and
  launcher arguments, Rust manifest verification and provisioning, the
  seven-key Python npm distribution contract, and
  `AIZIM_CLAUDE_EXECUTABLE`.
- Added the stable `claude` doctor check. It invokes only
  `ABSOLUTE_CLAUDE_PATH --version` in the scrubbed host environment, requires
  the exact output `2.1.218 (Claude Code)`, and makes no authentication or model
  request.
- Bumped only the npm distribution manifest schema to `2`; the platform
  manifest schema and event schema remain unchanged at `1`, while the platform
  manifest now requires distribution schema `2`.
- Extended local/global `--ignore-scripts` installation smoke coverage and the
  canonical CI-evidence check set with `local_claude_21218` and
  `global_claude_21218`.

### TDD evidence

Every observable below was captured during this attempt and is reproduced here
so this tracked report remains the durable evidence artifact after scratch logs
are removed.

| Scenario | Invocation | Binary observable | Captured artifact |
|---|---|---|---|
| Resolver RED | `node --test tests/npm/claude-resolution.test.mjs` before `lib/claude.mjs` existed | exit nonzero; `ERR_MODULE_NOT_FOUND` for `lib/claude.mjs` | `.superpowers/sdd/task-7-report.md` |
| Resolver GREEN | `node --test tests/npm/claude-resolution.test.mjs` | exit `0`; `6` passed, including wrong meta/native versions, missing optional package/file, non-executable file, and no-PATH-fallback cases | `.superpowers/sdd/task-7-report.md` |
| Node contract RED/GREEN | focused asset, launch, package, and evidence tests before/after implementation | RED exposed missing `claudeExecutable`, absent launcher argv, exit `70` instead of `78`, and missing evidence keys; GREEN passed `47` tests | `.superpowers/sdd/task-7-report.md` |
| Rust contract RED/GREEN | `cargo test --workspace --locked` before/after the Rust handoff | RED failed to compile on missing `claude_executable`; GREEN passed manifest `9` and provision `7` contract tests | `.superpowers/sdd/task-7-report.md` |
| Python contract RED/GREEN | `/opt/homebrew/bin/uv run --frozen pytest tests/unit/test_distribution_context.py tests/integration/test_cli_doctor.py -q` before/after the Python handoff | RED failed collection on missing `resolve_claude_executable`; GREEN passed `23` tests | `.superpowers/sdd/task-7-report.md` |
| Closed-target regression | temporarily substituted a forbidden `darwin-x64` Claude alias, ran `node --test tests/npm/package-contract.test.mjs`, restored the intended mapping, and reran | mutation failed the exact three-target assertion; restored source passed `1` targeted test | `.superpowers/sdd/task-7-report.md` |

An exploratory package-lock assertion initially treated Anthropic's transitive
optional `darwin-x64` package as forbidden. That diagnostic was discarded:
the requirement is that Aizim exposes no `darwin-x64` target mapping, not that
upstream metadata omit its own optional package. The authoritative restored
mapping test and package inspection both passed.

### Full verification

| Scenario | Invocation | Binary observable | Captured artifact |
|---|---|---|---|
| Node suite | `npm run test:node` | exit `0`; `88` passed, `0` failed | `.superpowers/sdd/task-7-report.md` |
| Rust formatting | `cargo fmt --all --check` | exit `0` | `.superpowers/sdd/task-7-report.md` |
| Rust lint | `cargo clippy --workspace --all-targets --locked -- -D warnings` | exit `0`; finished without warnings | `.superpowers/sdd/task-7-report.md` |
| Rust suite | `cargo test --workspace --locked` | exit `0`; `23` tests passed across cache, manifest, and provision contracts | `.superpowers/sdd/task-7-report.md` |
| Focused Python suite | `/opt/homebrew/bin/uv run --frozen pytest tests/unit/test_distribution_context.py tests/integration/test_cli_doctor.py -q` | exit `0`; `23` passed | `.superpowers/sdd/task-7-report.md` |
| Full Python suite | `/opt/homebrew/bin/uv run --frozen pytest -q` | exit `0`; `783` passed, `2` skipped, `1` explicitly deselected | `.superpowers/sdd/task-7-report.md` |
| Architecture suite | `/opt/homebrew/bin/uv run --frozen pytest tests/architecture -q` | exit `0`; `12` passed | `.superpowers/sdd/task-7-report.md` |
| Authorized cleanup regressions | focused workspace-backend and doctor exception tests | exit `0`; `14` passed | `.superpowers/sdd/task-7-report.md` |
| Python lint | `/opt/homebrew/bin/uv run --frozen ruff check src/aizim/runtime/distribution.py src/aizim/cli/doctor_command.py tests/unit/test_distribution_context.py tests/integration/test_cli_doctor.py tests/unit/test_codex_workspace_backend.py` | exit `0`; `All checks passed!` | `.superpowers/sdd/task-7-report.md` |
| Python types | `/opt/homebrew/bin/uv run --frozen ty check` | exit `0`; `All checks passed!` | `.superpowers/sdd/task-7-report.md` |
| No-excuse scans | Python and Rust touched-file no-excuse scans | exit `0`; Python reported no violations in `5` files and Rust passed `6` files | `.superpowers/sdd/task-7-report.md` |
| Build freshness | `npm run build` followed by `npm run check` | exit `0`; built `@aiz.im/aizim 0.1.0` for `darwin-arm64`; `All checks passed!` | `.superpowers/sdd/task-7-report.md` |
| Package inspection | `npm run pack` plus manifest/tar inspection | exit `0`; dependency `2.1.218`, exactly `3` Aizim Claude aliases, `darwin-x64=unsupported`, distribution schema `2`, platform schema `1`, platform distribution schema `2`, and `package/lib/claude.mjs` present | `.superpowers/sdd/task-7-report.md` |
| Packaged install smoke | `node scripts/npm/install-smoke.mjs` | exit `0`; `aizim 0.1.0`, `READY`, `SECURITY GATE PASS`, `AIZIM RUN PASS` | `.superpowers/sdd/task-7-report.md`; generated payload `build/npm/install-smoke-evidence.json` |
| CI evidence validation | parsed `build/npm/install-smoke-evidence.json` and ran `npm run check` | exit `0`; `local_claude_21218=true`, `global_claude_21218=true`, `aizim_run=true` | `.superpowers/sdd/task-7-report.md`; `build/npm/install-smoke-evidence.json` |
| Patch hygiene | `git diff --check` | exit `0`; no whitespace errors | `.superpowers/sdd/task-7-report.md` |

Pure non-empty line counts remained bounded:

- `src/aizim/runtime/distribution.py`: `176`
- `src/aizim/cli/doctor_command.py`: `220`
- `tests/unit/test_distribution_context.py`: `186`
- `tests/integration/test_cli_doctor.py`: `213`
- `tests/unit/test_codex_workspace_backend.py`: `139`

The doctor module is at the review-warning band; the next functional expansion
should extract provider version checks rather than growing the module further.
No behavioral refactor was warranted in this task.

### Manual QA

The exact installed native binary was resolved through the package-local
resolver and invoked with only `--version`, under an environment with
`PATH=/usr/bin:/bin` and no authentication/model variables:

```text
resolved=/Users/fwmbam4/CodeHub/WangFrankie/AI4M/Aizim/node_modules/@anthropic-ai/claude-code-darwin-arm64/claude
stdout=2.1.218 (Claude Code)
exit=0
```

The packaged local and global installs independently resolved and invoked their
own installed native binaries. Both exact-version doctor checks passed, as did
the Rust-to-Python distribution handoff and the real `aizim run` smoke.

### Scope and handoff

- The base was verified clean at exact commit
  `0c32bf56225146d89f54b97003f30489891b9ab6` before edits.
- Parent-authorized narrow amendments were:
  `scripts/npm/verify-ci-evidence.mjs` for the canonical evidence-name source;
  `tests/unit/test_codex_workspace_backend.py` for the seven-key npm fixture and
  its Claude executable; and replacing synthetic `RuntimeError` mocks with the
  production-caught `OSError` in that fixture and the owned doctor test.
- `scripts/npm/write-ci-evidence.mjs` required no direct edit because it consumes
  the canonical check-name list from `verify-ci-evidence.mjs`; the exhaustive
  writer behavior changed through that single source of truth.
- `lib/errors.mjs` required no direct edit because its existing generic
  `DistributionError` already carries the new stable
  `CLAUDE_PACKAGE_INVALID` code; launch classification is the owning change.
- The exact commit subject is
  `Carry the Claude executable through npm`. The enclosing commit SHA is
  recorded in the completion handoff because embedding a commit's own hash in
  its contents is not stable.

---

Date: 2026-07-21
Base HEAD: `e09bb0776c5842bb07199f5ae89811879e4dc5d7`

## Result

- Added the exact typed backend contracts plus a deterministic fake backend
  that uses the production framed gateway transport.
- Extended one-shot broker redemption into an optional authenticated,
  length-prefixed capability channel while preserving plain redemption.
- Added a low-level MCP 1.28.1 stdio sidecar with role-exact discovery, strict
  one-field envelopes, fixed public failures, disconnect termination, and
  token zeroing.
- Added the Codex 0.144.6 backend and sole new subprocess owner. Launches reuse
  the Task 6 sandbox overrides, pass the prompt only on stdin, require the MCP
  sidecar under strict config, bound stdout/stderr, validate the final schema,
  reap process groups, and always revoke capabilities and clean up.
- Added the hidden `aizim gateway sidecar` route. The existing
  `aizim-gateway-sidecar` console script now resolves to the implemented entry
  point.

## TDD evidence

- Initial RED collection exited 2 with four expected import failures: the two
  unit modules could not import `aizim.agents.backend`, and the two sidecar
  modules could not import `aizim.gateway.mcp_tools`.
- Dedicated RED regressions also reproduced malformed post-redemption frames
  poisoning broker close, an unhashable final status escaping as `TypeError`,
  and a replaced Codex image being accepted after backend construction.
- Review-driven RED regressions reproduced an in-flight transport close hang,
  response misassociation after cancellation, leaked sidecar tasks, skipped
  revocation under repeated cancellation, a model-writable parent result path,
  acceptance of weakened sandbox profiles, uncaught hostile JSON, blocked
  broker shutdown, cancellation-interrupted child reaping, and execution of a
  replaced Codex image by the sandbox compiler before identity rejection.
- Exact-SHA review of the initial Task 7 commit added RED regressions for a
  rehashed profile bound to unapproved project/developer roots, a TERM-ignoring
  same-group descendant surviving after its direct parent exited, and a
  consumed session ID remaining in Python `sys.argv`. Follow-up review found
  the hidden CLI route and `--session-id=value` form still retained that ID;
  a four-case RED matrix now proves both entry points scrub `sys.argv`, parser
  state, copied command lines, and outer Aizim stack frames before serving. The
  related pre-existing timeout test was also made readiness-ordered.
- Final Task 7 focused backend/sidecar/lifecycle suite:
  `58 passed in 6.83s`.

## Full verification

- Python 3.14 full suite: `342 passed in 22.63s`.
- Python 3.12 full suite: `342 passed in 21.68s`.
- Python 3.14 strict full suite with `PYTHONASYNCIODEBUG=1` and
  `PytestUnraisableExceptionWarning` promoted to errors:
  `342 passed in 23.06s`.
- Full-repository Ruff: passed.
- Full `ty check`: passed.
- Ruff format check for all 26 touched Python files: passed.
- `git diff --check`: passed.
- Architecture/import/subprocess/SQLite and pure-LOC gates: passed. The largest
  changed Python file is `src/aizim/gateway/session_broker.py` at 249 non-empty
  lines.
- `src/aizim/state/rpc.py` SHA-256 remained
  `f513efd625dd5abbe4f942e89024a571f5c4617fe6abcff4dbe4e51f8faebfcc`.

## Manual QA

- `aizim-gateway-sidecar --help` exposed the required broker socket and session
  ID arguments.
- `aizim gateway sidecar --help` reached the same hidden sidecar interface.
- The installed Codex reported `codex-cli 0.144.6`; its resolved executable
  SHA-256 was
  `134063e133f0b4244fa3b251acf973d4fe4b4aeeacbdc135211bf480f59f1477`.
- A launch spec generated by `build_codex_launch_spec` was passed to the real
  Codex CLI with `--strict-config exec --ignore-user-config --ignore-rules
  --help`; the compiled inline required MCP table parsed successfully without a
  model call.
- The same live launch spec placed the parent-owned final result in the
  model-read-only view and outside writable scratch.

## Scope and handoff

- Task 7 was committed as `366dbcd6315229af87b83dfde703059b7b949307`;
  exact-SHA review fixes were committed as
  `110533f5e2fde21b1912d0acbf6dfe698ba2555a` and
  `9691c16e33fff6cc3ed626b7aac519be1480e7c4`.
- The tracked worktree and index are clean at the latest review-fix SHA.
- Exact-SHA re-review of
  `9691c16e33fff6cc3ed626b7aac519be1480e7c4` approved Task 7 with zero
  Critical, Important, or Minor findings.
- No worktree was created.
- Changes are limited to the approved Task 7 paths and its ignored scope
  amendment. User-owned `.omc/` and `docs/superpowers/plans/` remain untracked
  and untouched.
- Darwin authenticates the Python interpreter image for the required console
  script. The explicit slices 1/2 threat assumption therefore keeps
  unsandboxed same-UID host processes in the trusted TCB; eliminating that
  assumption would require a native authenticated launcher. Task 6 separately
  proves the untrusted model sandbox cannot connect to the broker.
