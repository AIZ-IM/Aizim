# Task 8 post-commit verification

Verified commit:
`5f382672c61970b8eba6bb325134a191d071505f`
(`Connect the Claude controller backend`).

| Scenario | Invocation | Observable | Artifact |
|---|---|---|---|
| Exact commit and tracked status | `git rev-parse HEAD`; `git status --porcelain=v1 --untracked-files=no`; `git show -s` | exact SHA/subject; tracked status empty | `00-head.txt` |
| Task 8 focused behavior | `uv run --frozen pytest` over Codex controller, Claude controller, provider isolation, and supervisor integration | exit `0`; `36 passed in 13.46s` | `01-focused.txt` |
| Architecture and strictness | architecture pytest, Ruff format/check, `ty check`, no-excuse checker, pure-LOC audit, commit diff check | exit `0`; architecture `12 passed`; all strict checks passed; LOC `250/249/250/250/250` | `02-static.txt` |
| Actual installed Claude, no model | package-local `claude --version`, full mandated safety flags with terminal `--help`, and `auth status --json` with output discarded | exact `2.1.218 (Claude Code)`; flag parse exit `0`; host auth fixed outcome `READY` | `03-manual.txt` |
| Full Python suite | `uv run --frozen pytest -q` | exit `0`; `797 passed, 3 skipped in 148.14s` | `04-full-suite.txt` |

The manual invocation did not send a prompt or model request and did not
record the authentication payload. The `.omo` directory is intentionally
present after the commit because the completion-verification hook required
post-commit evidence under `.omo/evidence/`.
