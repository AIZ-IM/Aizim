# Task 9 stop-hook verification

Verified commit: `1221ddc0202b1891e1e05279a96921aa1d7713ef`

## Recorded scenarios

| Scenario | Invocation | Exit/observable | Artifact |
| --- | --- | --- | --- |
| Commit and tracked-state preflight | `git rev-parse HEAD`; `git status --short --branch`; `git diff --exit-code`; `git diff --cached --exit-code`; `git diff --check` | HEAD exact; tracked and staged diffs empty; only `.omo/` untracked | `00-preflight.txt` |
| Exact-SHA build | `npm run build` | Exit 0; wheel and darwin-arm64 distribution built | `01-npm-build.txt` |
| Complete package aggregate | `npm test` | Exit 0; Node 91 passed; Rust 23 passed; Python 805 passed, 1 skipped, 22 deselected | `02-npm-test.txt` |
| Installed-wheel controller loop | Executed inside `npm test` by `scripts/npm/install-smoke.mjs` | `CONTROLLER SMOKE PASS`; `assignment_executions=1`; `restart_duplicates=0` | `02-npm-test.txt` |
| Package validation | `npm run check` | Exit 0; `Checked npm distribution for darwin-arm64` | `03-npm-check.txt` |
| Package archives | `npm run pack` | Exit 0; host and meta tarballs listed | `04-npm-pack.txt` |
| Source controller loop | `/opt/homebrew/bin/uv run --frozen python scripts/qa/controller_smoke.py` | Exact three-line smoke output; one execution; zero duplicates | `05-source-controller-smoke.txt` |
| Credentialed controller boundary | `/opt/homebrew/bin/uv run --frozen pytest tests/e2e/test_real_controller_smoke.py -m manual_real_controller -q -rs` | Two explicit `AUTH_UNAVAILABLE` skips; not claimed as passes | `06-manual-controller-gate.txt` |
| Fresh package consumer | Mode-0700 temporary directory; `npm install --ignore-scripts` both generated tarballs; installed `aizim init`, `aizim --version`, and `aizim doctor --json` | Exact version; doctor ready; Codex `0.145.0` PASS; Claude `2.1.218` PASS; no global fallback | `07-fresh-consumer.txt` |
| Temporary consumer cleanup | Move exact temporary consumer to macOS Trash and verify original path absent | `consumer_cleanup=verified_absent_recoverable_trash` | `08-consumer-cleanup.txt` |
| SHA-bound payload audit | Parse build, test, install-smoke, and pack JSON; compare each SHA to final HEAD; validate `controller_loop` | All four payloads equal final HEAD; `controller_loop=true`; every cited artifact nonempty | `09-final-verdict.txt` |

## Commits

- `3ff5fe244bc2f7baf098d3a1f14ae612b2ce3777 Document and verify controller operation`
- `1221ddc0202b1891e1e05279a96921aa1d7713ef Fix package-local Codex sandbox probe`

## Residual concern

The independently reproduced CPython 3.14 frozen-exception traceback defect is
outside Task 9 scope and remains recorded at
`../45-frozen-exception-repro.txt`.
