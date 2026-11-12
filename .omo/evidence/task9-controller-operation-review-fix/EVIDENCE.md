# Task 9 code-review correction evidence

Final SHA: `87640901c7448206b6363e9d39e095e70b6c8c70`

Corrective commit: `87640901c7448206b6363e9d39e095e70b6c8c70 Mark controller smoke safety boundaries`

The corrective commit changes only the two requested safety-boundary comments in
`scripts/qa/controller_smoke.py`. Runtime behavior and exact smoke output are
unchanged.

| Success criterion | Exact scenario and invocation | Binary observable | Captured artifact |
|---|---|---|---|
| Strict no-excuse compliance | `check-no-excuse-rules.py` with the 10 Task 9 and approved-follow-up Python paths listed in the artifact | Exit 0; `no violations in 10 file(s)` | `00-no-excuse-green.txt` |
| Python static gates | `uv run --frozen ruff format --check scripts/qa/controller_smoke.py`; `uv run --frozen ruff check scripts/qa/controller_smoke.py`; `uv run --frozen ty check scripts/qa/controller_smoke.py`; `git diff --check` | Every command exited 0 | `01-static-green.txt` |
| Focused controller regression | `uv run --frozen pytest tests/integration/test_cli_control.py tests/integration/test_cli_control_projection.py tests/integration/test_cli_control_ownership.py tests/e2e/test_real_controller_smoke.py tests/architecture/test_authority_imports.py -m 'not manual_real_controller' -q` | Exit 0; `60 passed, 2 deselected` | `02-focused-60-green.txt` |
| Pure-LOC ceiling | Project pure-LOC counter over the same 10 scoped Python paths | All values `<=250`; maximum 250 | `03-pure-loc.txt` |
| Source smoke contract | `uv run --frozen python scripts/qa/controller_smoke.py` with byte assertions | Exit 0; exact three stdout lines; zero stderr bytes; one execution and zero restart duplicates | `04-source-smoke-green.txt` |
| Distribution build | `npm run build` on the final SHA | Exit 0; wheel and `darwin-arm64` package built | `05-npm-build.txt` |
| Aggregate tests and installed smoke | `npm test` on the final SHA | Exit 0; Node 91 passed; Rust 23 passed; Python 805 passed, 1 skipped, 22 deselected; installed smoke one execution and zero duplicates | `06-npm-test.txt` |
| Distribution static/package check | `npm run check` on the final SHA | Exit 0; both check stages passed and distribution checked | `07-npm-check.txt` |
| Package assembly | `npm run pack` on the final SHA | Exit 0; both platform and root tarballs emitted | `08-npm-pack.txt` |
| Credentialed controller gate | `uv run --frozen pytest tests/e2e/test_real_controller_smoke.py -m manual_real_controller -q -rs` | Exit 0; two literal `AUTH_UNAVAILABLE` skips, not claimed as passes | `09-manual-controller.txt` |
| Fresh package consumer | Mode-0700 `mktemp` consumer; `npm install --ignore-scripts` both tarballs; installed `aizim --version`, `aizim init`, and `aizim doctor --json` | Exact `aizim 0.1.0`; doctor ready; exact Codex `0.145.0` and Claude `2.1.218`; no global fallback | `10-fresh-consumer.txt` |
| Temporary consumer cleanup | `/usr/bin/trash` on the exact recorded temporary consumer directory, followed by absence check | Exit 0; `cleanup_absent=true` | `11-consumer-cleanup.txt` |
| Exact-SHA payload and repository audit | Compare all four generated payload SHA fields to `git rev-parse HEAD`; require `controller_loop=true`; run `git diff --check`, tracked/cached diff checks, and `git status --short` | All payload SHAs equal final SHA; controller loop true; tracked and staged diffs empty; only `.omo/` untracked | `12-final-audit.txt` |

Ruff reports informational warnings because the repository's project-specific
`ANYIO_OK` and `BROAD_EXCEPT_OK` no-excuse markers are not Ruff rule codes.
Ruff format and lint both exit 0; the strict project checker independently
recognizes the markers and exits 0.

The separately recorded CPython 3.14 frozen-exception/context-manager defect is
unchanged and remains outside this narrowly authorized review correction.
