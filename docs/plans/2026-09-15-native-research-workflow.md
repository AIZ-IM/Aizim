# Native research workflow

Status: implemented and validated on Linux. See
[the validation record](../operations/research-validation-2026-09-15.md) for executed checks
and the limits of the real-model test.

## Scope

Extend the existing trusted state, Lean, capability, and promotion boundaries with:

- ordinary Lean project layouts and configurable Worker capacity;
- immutable research targets, dependency scheduling, bounded repair rounds, and restart recovery;
- typed, searchable research memory and offline Lean declaration search;
- an asynchronous operator inbox, consumed at round boundaries;
- a local read-only dashboard with progress and measured usage;
- human contribution records, source citations, semantic reviews, and an exportable research record.

All durable writes remain owned by `StateService`. Model output cannot mark a theorem verified.
Completed assignment versions remain immutable; another attempt uses a new version. Search indexes
and dashboards are derived views. Unknown usage or prices remain unknown. Nothing is published to
an external service automatically.

## Human roles

Record problem formulation, definitions, strategy, counterexamples, semantic review, reusable
library design, and exposition as separate contributions. A mathematical validity result does not
establish originality, authorship, identity, or priority. Operator names are attribution supplied
by the local operator, not authenticated scholarly identities. Guidance and review are explicit
events; autonomous and collaborative participation must not be conflated.

Kevin Buzzard's 2026-09-04 account emphasizes reusable Mathlib contributions and a human-readable,
explorable account of the modern proof after a complete automatic formalization became available.
The implementation should preserve these distinct outcomes rather than collapse them into one
"proved" flag.

Sources:

- https://xenaproject.wordpress.com/2026/09/04/flt-anthropic-has-beaten-me-to-it/
- https://www.anthropic.com/research/formalizing-fermats-last-theorem
- https://github.com/frenzymath/Danus
- https://github.com/frenzymath/Rethlas
- https://github.com/frenzymath/Archon
- https://github.com/frenzymath/Archon-Horizon

## Verification

Exercise non-smoke projects, failed-then-successful repair, restart without duplicate execution,
dependency blocking, concurrent independent targets, scoped memory, inbox acknowledgement,
immutable target signatures, measured token accounting, and dashboard escaping. Run the existing
regression and provider/sandbox contracts. Distinguish deterministic tests, actual Lean checks,
and credentialed model runs in the handoff.
