# Task 9 controller operation evidence

Final HEAD: `1221ddc0202b1891e1e05279a96921aa1d7713ef`

| Success criterion | Exact scenario and invocation | Binary observable | Captured artifact |
| --- | --- | --- | --- |
| Projection TDD | Focused controller/worker projection tests | Initial 23 failures; final 60 passed | `03-projection-red.txt`, `25-loc-fix-focused.txt` |
| Test split preservation | Compare normalized pre/post pytest collection inventories | 26 before, 26 after, zero duplicates | `22-inventory-exact.txt` |
| Source controller loop | `/opt/homebrew/bin/uv run --frozen python scripts/qa/controller_smoke.py` | Exit 0, exact three-line stdout, stderr bytes 0 | `31-source-smoke-final.txt` |
| Full Python | `/opt/homebrew/bin/uv run --frozen pytest -m "not manual_real_codex and not manual_real_controller" -q` | 823 passed, 2 skipped, 3 deselected | `26-python-full-nonmanual-green.txt` |
| Static quality | Ruff on repository, ty check, diff check | All checks passed | `33-final-static-repo.txt` |
| Node and Rust | `npm run test:node`; Cargo fmt, clippy, test | Node 91 passed; Rust fmt/clippy exit 0; 23 Rust tests passed | `27-node-tests.txt`, `28-rust-fmt.txt`, `29-rust-clippy.txt`, `30-rust-tests.txt` |
| Credentialed QA boundary | Pytest manual controller marker with skip reasons | 2 skipped with `AUTH_UNAVAILABLE` | `32-manual-real-controller-final.txt` |
| Package-local Codex regression | Package-local Codex PATH, exact isolated macOS sandbox node | RED exit 127 with safe hashes; GREEN 1 passed | `44-package-local-codex-toggle.txt`, `46-package-local-codex-green.txt` |
| Task 8 security regression | Provider-isolation/architecture focused and macOS security suite | 16 passed; 61 passed, 1 deselected; file 250 pure LOC | `47-task8-focused-green.txt`, `48-task8-security-green.txt` |
| Exact-SHA package build | `npm run build` at final HEAD | Exit 0, darwin-arm64 package built | `49-final-npm-build.txt` |
| Exact-SHA package aggregate | `npm test` at final HEAD | Node/Rust/static/Python green; Python 805 passed; install smoke exact one execution and zero duplicates | `50-final-npm-test.txt` |
| Package check and pack | `npm run check`; `npm run pack` | Both exit 0; host and meta tarballs listed | `51-final-npm-check.txt`, `52-final-npm-pack.txt` |
| Fresh consumer | Mode-0700 temp consumer; `npm install --ignore-scripts` both tarballs; installed version and initialized doctor | Mode 0700; exact `aizim 0.1.0`; doctor ready; Codex 0.145.0 and Claude 2.1.218 PASS without global fallback | `53-fresh-consumer.txt`, `54-fresh-consumer-initialized-doctor.txt` |
| Residual frozen exception | Actual `ControllerBackendError` through stdlib context manager on CPython 3.14.6 | `FrozenInstanceError: cannot assign to field '__traceback__'` | `45-frozen-exception-repro.txt` |

Commits:

- `3ff5fe244bc2f7baf098d3a1f14ae612b2ce3777 Document and verify controller operation`
- `1221ddc0202b1891e1e05279a96921aa1d7713ef Fix package-local Codex sandbox probe`
