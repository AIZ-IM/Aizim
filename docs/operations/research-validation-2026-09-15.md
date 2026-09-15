# Research workflow validation — 2026-09-15

## Implemented surface

- Ordinary Lean projects with TOML or Lean Lake configuration, and a generated research library
  inside the supervisor's run copy.
- Immutable targets, dependency ordering, concurrent ready tasks, bounded repair rounds, and
  recovery that settles existing publication evidence without repeating completed work.
- Scoped research memory, offline declaration search, and operator guidance consumed at round
  boundaries.
- Contribution and review records separating formal validity, intended meaning, research value,
  library design, exposition, and declared attribution.
- A loopback-only read-only dashboard, standalone HTML/JSON exports, and measured usage accounting.

## Real model check

A fresh project containing `ResearchLab.lean` was initialized and given this target:

```lean
(n : Nat) : n + 0 = n
```

Controller and Worker both used `gpt-6-astra` through the attested native Codex CLI `0.154.0`.
The no-model security gate passed, one research round completed, and the target reached
`verified` through the Lean `4.32.1` promotion path. The command exited successfully.

Measured usage across that round's Controller and Worker was 213,783 input tokens, including
168,320 cached input tokens, and 655 output tokens. No price table was supplied, so monetary
estimates remain unknown. This is a small integration check, not evidence of research-level
mathematical discovery or general proving capability.

## Additional checks

- Deterministic regression cases cover failure then repair, dependency blocking, bounded
  concurrency, restart recovery, immutable targets, scoped memory, and guidance arriving during
  a round.
- Public CLI tests cover live-state mutations, human contribution/review records, local search,
  pause/resume, private exports, and rejection of proof text in a target statement.
- Actual Linux model-tool execution denied project files, copied authentication files, inherited
  secret environment variables, and network access, while permitting scratch writes.
- The provider-domain proxy admitted an unauthenticated OpenAI endpoint request and rejected an
  unlisted destination. Codex's API driver uses its own client connection; the proxy is used for
  outer transport isolation where supported, including the Claude adapter.
- Browser checks exercised the live dashboard, filters, dependency graph, contribution records,
  JSON endpoint, host-header checks, and rejection of POST requests.
- The built wheel contains the research runtime, standalone dashboard asset, and provider-compatible
  controller schema. The npm contract checks pass.

Final platform-independent regression: **857 passed, 3 skipped, 55 deselected**.
The separate real-Lean checks passed for both Lake configuration formats; the npm contract
suite passed 35 tests, and the external Codex/Linux sandbox lane passed 8 tests with the
unselected Claude case skipped. Ruff, ty, wheel-content verification, and browser checks passed.

macOS acceptance and a credentialed Claude model run were not executed on this Linux host.
Large-project throughput and long-duration provider authentication are not established by
the small integration check.
