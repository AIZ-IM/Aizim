# Erdős #7 research run — 2026-11-09

## Objective and formulation

Investigate [Erdős problem #7](https://www.erdosproblems.com/7), also indexed as JSP-000047: can a finite collection of congruence classes with distinct odd moduli greater than one cover every integer? Both existence and nonexistence remain open. The Aizim target, `Erdos7.NoOddCover`, asks for the nonexistence direction; it is a conjecture, not an established theorem.

The isolated Lean project defines coverage over natural numbers. For finitely many positive moduli, coverage of the natural numbers and the integers is equivalent by periodicity modulo their least common multiple. This equivalence was checked mathematically, but is not itself formalized in the project. The Lean definitions compile with Lean 4.35.0.

## Independent preliminary work

A direct prime-sieve density argument excludes systems whose moduli are all at most 103. For odd primes `p <= 103`, adding one arbitrary class modulo every such prime can only increase the covered set. The density of integers surviving these classes is `P = product (1 - 1/p) > 0` by the Chinese remainder theorem. Within the surviving set, a class modulo an odd composite `n <= 103` covers at most the relative fraction `(1/n) * product_{p|n} p/(p-1)`. Exact rational arithmetic gives a sum of `94043/95040` over those composites, leaving at least `P * 997/95040` uncovered. The fraction was evaluated exactly; the Chinese remainder counting formula was also checked by enumeration for several choices of residues in a smaller period. This bound is partial, its novelty has not been established, and it is not a Lean-verified result.

Known work is substantially broader: the [problem reference](https://www.erdosproblems.com/7) cites a proof that distinct odd squarefree moduli cannot cover, and says that any odd covering system has an LCM divisible by 9 or 15. A [2026 preprint](https://arxiv.org/abs/2607.25628) reports a kernel-checked exclusion for LCM at most 10,000; this was not independently rerun here.

## Actual Aizim run

- Isolated project: `/tmp/aizim-erdos7-NojT0jBf` (temporary storage; may be removed by the operating system).
- Target: `erdos7-no-odd-cover`; source reference: https://www.erdosproblems.com/7.
- Model selected by the user's omp/magpie configuration: `magpie/netcup-magpie/group/omp-main`.
- Toolchain: Lean 4.35.0, omp 18.18.2, magpie 0.1.3338. `aizim doctor` reported READY; the security gate passed. Its report states that network destination isolation is not enforced.
- Run ID: `research-01m6ys53yrgyd8cxxspt3stzt8`; three bounded attempts, all failed; target status `blocked`.
- No declaration was verified or published. All three candidate files remained the initial theorem skeleton containing `sorry`. The only saved round summaries were `Round failed: WorkerExecutionError.` The report recorded usage as unknown for all three attempts; actual token usage and cost were not measured.
- The local JSON export is `/tmp/aizim-erdos7-NojT0jBf/report.json`.


## Gateway correction and bounded recheck

`project.read` is registered by the worker gateway. It authorizes paths using the original project
source-file allowlist and reads them from the supervisor-owned run copy. This prevents generated
worker documents in the run copy from being exposed as project sources. It also rejects hidden
paths, traversal, symbolic links, and files larger than 512 KiB. Capability checks and the
prohibition on publishing `sorry` remain in force. The worker-host integration checks passed
(8 tests), including a regression for another worker's generated document; Ruff passed on the
modified code and test.

One additional attempt on the same conjecture ran after the initial gateway change (run
`research-01m6yvcn1smj5h0qxe7ax894n5`). It also ended in `WorkerExecutionError`, without verified
declarations. The recorded worker events do not contain the earlier `project.read` unregistered
denial, but do not establish that the model called `project.read` in this attempt. After this run,
the implementation was tightened to read the frozen run copy and was verified by the integration
checks; that final change was not followed by another model run. The expanded JSON export is
`/tmp/aizim-erdos7-NojT0jBf/report-after-fix.json`. The generic worker failure still needs more
diagnostic evidence; no root cause is established for the additional attempt.

In each of the first three attempts, `project.read` returned `TARGET_UNREGISTERED`: the worker was advertised that capability, but the target was not registered by the research gateway. The initial process output also reported `FORBIDDEN_LEAN_TOKEN`; rejecting a candidate that still contains `sorry` is the intended safety behavior. No model-generated mathematical proof or partial proof was retained in the three candidate files.

## Outcome

The original open problem is not solved. The preliminary bound is independent of the failed Aizim run. A verified contribution requires the exact target to pass the trusted Lean publication path; a model response, a successful Lean tool invocation, or a bounded computation alone is insufficient.
