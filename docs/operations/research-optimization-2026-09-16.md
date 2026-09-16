# Research retrieval and notebook optimization — 2026-09-16

Baseline: `62b52bc`. This change improves declaration retrieval and the existing research
reading interface. It adds no model provider or research-state writer.

## Changes

- Replace the repeated whole-corpus document-frequency scan with a reusable BM25 search
  structure. Split snake_case/camelCase names and rank names, types, and docstrings.
- Cache source declarations in private JSON files under `.aizim/search/`. Unchanged source
  files are reused across processes; file and project-control changes invalidate cached data.
  Source discovery handles nested comments, strings, namespaces, attributes, and multiline
  signatures, and is explicitly marked as best-effort discovery.
- Add `research index --compiled --module ...` using Lean's own imported environment to
  obtain full names and elaborated types. Check the pinned toolchain and existing Lake build
  traces before extraction. Keep source fallback for modules outside the extracted closure
  and whenever the compiled snapshot becomes stale. Custom Lake `srcDir` is covered.
- Add a target research notebook to the dashboard and standalone HTML export, bringing
  formulation, definitions, strategy, explanations, guidance, reviews, and evidence together.
  Review applicability checks both target identity and declaration evidence. Keyboard
  navigation, fragment links, mobile layout, and unchanged-refresh preservation are included.

Implementation entry points:
[search](../../src/aizim/research/search.py),
[source cache](../../src/aizim/research/declaration_index.py),
[compiler indexing](../../src/aizim/research/compiled_index.py),
[Lean extractor](../../src/aizim/research/ExtractDeclarations.lean),
[research notebook](../../src/aizim/research/dashboard.html).

The design follows the declaration-analysis and retrieval ideas discussed in
[the project landscape review](../designs/2026-09-15-ai-lean-project-landscape.md).
The Lean helper uses the APIs supplied by the project's pinned Lean installation.

## Measured retrieval result

Command, from this repository's Python environment:

```sh
python scripts/benchmark_research_search.py --baseline-ref 62b52bc --size 5000 --repeat 3
```

| Measurement | Seconds |
| --- | ---: |
| Baseline ranking, median of 3 queries | 3.291988 |
| New search-structure construction | 0.054023 |
| New cached query, median of 3 | 0.003954 |
| New construction + median query | 0.057977 |

The top 10 results are identical for this deterministic synthetic query. The cached query is
about 833 times faster in this workload; including initial construction gives about 57 times.
This isolates ranking on 5,000 synthetic declarations. It does not measure filesystem scanning,
compiler extraction, model inference, semantic retrieval quality, or theorem-proving speed.

## Validation

| Check | Result |
| --- | --- |
| Source-index, research workflow, public CLI, package and project-layout regression | 41 passed |
| Real Lean compiler indexing, normal layout and custom `srcDir` | 2 passed |
| Existing real Lean research proof workflow, TOML and Lean Lake configurations | 2 passed |
| Standalone browser acceptance: selection, filtering, keyboard, review binding, fragment restore, mobile layout | 8 checks passed |
| Live browser acceptance: preserve open evidence on unchanged refresh, display real evidence, render updates, preserve untrusted text as text | 4 checks passed |
| Ruff, type checking, whitespace checks | Passed |
| Python wheel build and packaged asset/source comparison | Passed |

The compiler test checks an actual declaration and its docstring/type, dependency filtering,
source fallback outside the selected module closure, invalidation after source edits, and
rejection of stale Lake builds. A development fixture imported 74,122 declarations from
`ResearchLab` and its transitive imports; this count is extraction, not new proof generation.

The first combined Lean run used the public `/tmp` and hit the existing workspace-view privacy
check in the two proof-workflow tests. Re-running those tests with the required private
`TMPDIR` and pinned tools passed. The compiler tests passed in both runs' applicable cases;
no workspace-view policy was relaxed.

Reproduction entry points:

```sh
pytest tests/unit/test_research_search.py tests/unit/test_research_workflow.py \
  tests/integration/test_research_cli.py tests/unit/test_package.py \
  tests/unit/test_project_layout.py -q

# Put the pinned Lean/Lake on PATH; use the private TMPDIR from the foundation runbook.
pytest tests/integration/test_compiled_research_index.py \
  tests/integration/test_research_lean.py -q
```

Local QA output is under `build/optimization-2026-09-16/`, including the benchmark JSON,
test logs, browser checks, notebook export/screenshot, and wheel. These generated files are
ignored by Git. The browser fixture displays an existing recorded research run; this change
did not perform a new paid model evaluation.

## Current limits

Compiler extraction is an explicit command for an already built, trusted Lean 4.32.1 project.
Its imported environment extensions run as part of that operation. Search `origin` labels
describe how retrieval data was obtained; they do not constitute verification attestations.
The source fallback and type-wildcard mode remain textual tools. No embedding model, external
kernel integration, browser editing channel, or automatic proof-refactoring stage is included.

See [the updated research workflow](research-workflow.md) for user-facing commands.
