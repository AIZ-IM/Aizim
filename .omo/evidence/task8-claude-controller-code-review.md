# Code review: Task 8 Claude controller backend

Reviewed commit: `5f382672c61970b8eba6bb325134a191d071505f`

Base: `ddda9786030621d238fcc7ced8647c23680bf4ad`

## Result

`codeQualityStatus: CLEAR`

`recommendation: APPROVE`

## Findings

### CRITICAL

None.

### HIGH

None.

### MEDIUM

None.

### LOW

None.

## Review notes

- The full committed diff is a clean descendant of the supplied base and has no whitespace errors.
- `ClaudeControllerBackend` builds the required argv, uses the canonical controller-context bytes, parses only `structured_output`, rejects duplicate JSON object keys, pins executable/version/hash, validates the Codex sandbox executable/hash, uses the private workspace and filtered environment, and maps production dispatch exhaustively.
- The supervisor selects Claude only for the controller; `worker_backend=create_codex_backend` and the existing worker-model requirement remain intact. Preflight runs before `start_controller`, hence before an assignment can be claimed.
- The amendment to `permission_profile.py` is scoped to the two direct Anthropic variables. No unrelated provider-environment widening was found.
- All eight changed Python files meet the 250 pure-LOC ceiling (the largest changed files are exactly 250).
- Skill-perspective check: ran. The `remove-ai-slops` pass found no deletion-only, tautological, prompt-prose, implementation-mirroring, or needless-complexity tests. The `programming` pass found no new untyped escape hatch, unnecessary abstraction, or redundant production parsing/validation; the necessary untrusted JSON parse remains at the provider boundary. No violation of either perspective was found.

## Verification performed

- `git merge-base --is-ancestor ddda9786030621d238fcc7ced8647c23680bf4ad 5f382672c61970b8eba6bb325134a191d071505f` -> exit 0.
- `git diff --check ddda9786030621d238fcc7ced8647c23680bf4ad 5f382672c61970b8eba6bb325134a191d071505f` -> exit 0.
- Focused required suite -> `36 passed`.
- Required Ruff command -> `All checks passed!`.
- `uv run --frozen ty check` -> `All checks passed!`.
- `tests/unit/test_controller_decision.py` -> `27 passed`; an independent direct probe also rejected duplicate envelope and nested structured-output keys with `CONTROLLER_RESULT_INVALID`.
- Production-factory smoke with the locally resolved Claude `2.1.218 (Claude Code)` -> successful construction and executable identity pin.

## Evidence treatment and residual risk

The pre-existing `task-8-report.md` and `.omo/evidence/task8-postcommit-verification/03-manual.txt` claim host credential readiness. They were not used as credentialed-backend evidence: neither `ANTHROPIC_API_KEY` nor `CLAUDE_CODE_OAUTH_TOKEN` is directly present in this review environment, and the sandbox cannot be assumed to read host credential storage. Credentialed real backend execution is therefore `SKIPPED_AUTH_UNAVAILABLE`, not passed. This is a stated residual validation risk, not a defect in the committed implementation.

Worktree hygiene: the only observed untracked path is the pre-existing ignored `.omo/` evidence area; no source or test file was modified by this review.
