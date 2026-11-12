# Research workflow

Research targets connect an immutable Lean statement to a bounded execution loop, dependencies,
research memory, human guidance, and verification evidence. The foreground process owns the same
`StateService`, capability gateway, document leases, and promotion service as the original smoke
workflow. `lakefile.toml` and `lakefile.lean` projects are supported. The supervisor adds an
`AizimResearch` library inside its run copy; source project files remain the frozen input.

Install Lean 4.35.0, omp 18.18.2 and magpie 0.1.3338. Configure magpie and select the
model in the omp configuration backed by its local gateway; Aizim does not copy provider
credentials or choose a model. Install project dependencies with Lake before initialization when
the project uses external packages. See the [architecture contract](../designs/2026-11-08-omp-magpie-research-architecture.md).

In the omp user configuration (`~/.omp/agent/config.yml`), `modelRoles.default` selects
`magpie/<model-id>`. Its `~/.omp/agent/models.yml` entry for `providers.magpie` supplies a local
gateway `baseUrl` (for example `http://127.0.0.1:3425/v1`), `api: openai-completions`, and the
model catalog. Aizim stages only the selected model and local gateway settings into each private
attempt; provider credentials stay with magpie. Follow the [omp installation instructions](https://github.com/can1357/oh-my-pi)
and the [magpie setup guide](https://usemagpie.ai/docs/start), then select the omp model in
magpie's Agents tab. Start the configured gateway (for example with `magpie serve`) before `doctor`.
Aizim's npm launcher provisions its Python runtime; omp, magpie, and Lean are separate prerequisites.
The managed environment pins `lean-lsp-mcp==0.33.0`, `leanclient==0.13.2`, and
`mcp[cli]==2.2.0`. MCP 2.2.0 is an exact compatibility constraint for lean-lsp-mcp 0.33.0, not a
minimum version. The external Lean host is used for research and Lean integration only; it does
not participate in npm release qualification.

```sh
uv run aizim init /absolute/path/to/project
uv run aizim doctor --project /absolute/path/to/project

uv run aizim research task add --project /absolute/path/to/project \
  --task-id addition --title 'Addition identity' \
  --statement '(n : Nat) : n + 0 = n' --import Std \
  --author 'Researcher name' --source-ref 'local:research-notes' \
  --max-rounds 6 --max-failures 3 --timeout-seconds 900 --call-budget 400

uv run aizim research run \
  --project /absolute/path/to/project --max-seconds 1800
```

The statement contains the proposition, or binders followed by its result type, and no proof.
New dependency targets are added after their dependencies using repeated `--depends-on TASK_ID`.
Independent ready targets run concurrently, bounded by `resources.max_proof_workers` and
`resources.scratch_slots` (1–64). One shared Lean runtime remains serialized for formal operations.
`research run --watch` accepts later tasks and guidance without exiting when the frontier is idle.
The proof worker can read original project source files through `project.read` from the
supervisor-owned run copy. Authorization uses the original project's source-file allowlist;
generated worker documents are not made readable by their presence in the run copy. Reads are
limited to Lean sources and Lake/toolchain control files; hidden paths, symbolic links and files
larger than 512 KiB are rejected.


## Declaration search and indexing

Search keeps a disposable local index under `.aizim/search/`. Unchanged source files are reused
across queries and CLI processes. Source edits, additions, removals, and Lake/toolchain control
changes invalidate the relevant data. Search results include `full_name` and an `origin` field:
`source` means best-effort source discovery; `compiler` means a type extracted from an already
built Lean environment. Both are retrieval hints; formal verification still uses promotion.

```sh
uv run aizim research index --project /absolute/path/to/project
uv run aizim research search --project /absolute/path/to/project 'add zero'
uv run aizim research search --project /absolute/path/to/project \
  'ResearchLab.addition_identity' --mode name --project-only
```

The text ranker splits snake_case and camelCase names and weights names, types, and docstrings.
It computes document frequencies once and reuses its in-memory search data. `--mode type` is
textual wildcard matching (`_` or `?name`), not Lean type unification or semantic vector search.
Use `research index --rebuild` to reparse source files explicitly.

For fully qualified names and elaborated types, first build the project's chosen modules with
its existing toolchain, then extract a compiler index:

```sh
# Build with the project's installed, pinned toolchain.
lake --dir /absolute/path/to/project build
uv run aizim research index --project /absolute/path/to/project \
  --compiled --module ResearchLab --lake /absolute/path/to/toolchain/bin/lake
```

Repeat `--module` for multiple roots. The extractor imports their compiled dependency closure;
ordinary source discovery remains available for other project modules. `search --project-only`
filters out library dependencies. The compiled index supports the pinned Lean 4.35.0 toolchain;
`--lake` may be omitted when the matching native Lake executable or installed Elan toolchain is
available. The command checks Lake's existing build traces with `--no-build --no-cache`, and
rejects stale builds. Importing a compiled project loads its Lean environment extensions, so
this is an operator command for the same trusted project used by the normal build workflow.

Source and recorded build-artifact changes invalidate the compiled cache. Searches then use
source discovery until `research index --compiled ...` is run again. The compiler index does
not publish declarations or turn a source entry into proof evidence.

## Reading a research record

The dashboard and HTML export include a research notebook for each target. Select a target
or a dependency-graph node to read its formulation, definitions, strategy, counterexamples,
explanation, guidance, reviews, and formal evidence together. Target selections have shareable
fragment links within the local page; keyboard navigation and narrow screens are supported.

Execution status and semantic review are shown separately. A review applies to the current
evidence only when its target hash and declaration IDs match; otherwise its historical record
is still visible with an earlier-evidence label. An unchanged live refresh preserves open
evidence sections. This reading interface uses existing records and remains read-only.

## Verification and recovery

Before real research execution, the no-model security gate must pass. A worker submission is not
a verified result: only a `DeclarationPublished` event from the existing Lean promotion path can
settle a target as verified, provided the contribution's candidate identity and complete type match
the immutable target. An agent summary or human review cannot do so. Failed rounds retain feedback
and start another bounded attempt. Restart recovery settles already-published work without repeating
it, and interrupts unfinished attempts before continuing. Completed target statements, imports,
dependencies, and source references are immutable. Create another target for a different question.
Submission, settlement, and restart recovery bind the persisted candidate source to the first
durable attempt context. Its ordered imports must equal the target imports followed by the
already-published dependency modules at attempt creation, with duplicates removed; source imports
and submission metadata must match that context exactly. Historical non-research events remain
replayable. A historical research publication without its source artifact or trusted attempt
context cannot settle a target.

Each attempt snapshots its target's gateway `call_budget` (default 400, range 1–10000), states
that budget in its instructions, and mints it into its capability. The gateway refuses further
calls after it is spent (`WORKER_BUDGET_EXHAUSTED`); this counts as an ordinary failed round.
`task resume --call-budget N` may raise the allowance for later attempts without changing the
immutable target hash or an already-running attempt's allowance. Rate limiting instead returns
`GATEWAY_RATE_LIMITED` with a retry hint: the worker bridge backs off and retries within the
attempt deadline. These throttles remain audited as `CapabilityDenied` but consume no call
budget and are excluded from refusal counts. Failed rounds retain the agent's closing report,
measured usage, stop reason, and gateway error/refusal counts for the next attempt's guidance.

A submission can still be in promotion when its round closes. That round is recorded as awaiting
publication. If Lean later publishes the submission, or the resubmission that promotion makes when
the knowledge base moved on, the supervisor settles the target as verified and lifts any pause; a
rejected submission simply clears the mark. A later unverified round never downgrades a verified
target. Ending a run cancels promotions in progress, so a run without `--watch` lets awaiting
submissions settle for up to the largest target timeout, capped at 900 seconds. A closed round
also gives its own publication up to its target timeout, capped at 900 seconds. Research target
timeouts default to 900 seconds (range 1–14400). Every promotion consumer, including the smoke
conductor, drains queued submissions at startup and every 15 seconds, and can take over a claim
whose owner stopped heartbeating a minute ago.

The worker timeout starts after proof and scratch slots are reserved and bounds document setup,
gateway authority setup, and backend execution together. Waiting for those slots creates no lease
or capability. The supervisor gives each document lease that directive's timeout as its lifetime;
its capability and gateway session share the lease expiry. This covers the entire execution
deadline, including 14400-second directives, without a fixed extended grant. Completion, failure,
timeout, and cancellation still release the lease and revoke its capabilities.

A backend/model outage, an unexpected supervisor error, or a round that exceeds its worker
timeout plus publication grace and 120 seconds of overhead is an infrastructure failure. It
uses neither `max_rounds` nor `max_failures`. Interrupted attempts are also refunded. Attempt
sequence numbers remain monotonic across outages and restarts, independently of charged rounds.
Worker timeouts, spent call budgets and abstentions are proof failures and consume both limits.
Three consecutive infrastructure failures pause a target. Across a run, three consecutive
infrastructure failures stop new scheduling with run reason `infrastructure_unavailable`, also
under `--watch`; active attempts finish within their existing bounds. Ready targets without a
recent outage are tried before retrying an affected target. A completed proof attempt resets the
run streak. Check the worker environment before resuming targets or starting another run.


For an opt-in end-to-end smoke using the installed Lean toolchain, omp, a running magpie gateway,
and an actual configured model, run:

```sh
AIZIM_REAL_OMP=1 uv run pytest -m manual_real_omp tests/e2e/test_real_omp_smoke.py
```

This test makes real model calls and requires the external prerequisites above.

The default run exits once no eligible work remains and its submissions have settled, or once the
publication wait ends. Exit code `5` means targets remain pending,
blocked, paused, or running. SIGINT/SIGTERM cancel active work and retain the research record.

## Human guidance and contributions

Guidance is read at round boundaries. A pause takes effect after the active attempt settles;
SIGINT/SIGTERM are the immediate run cancellation controls.

```sh
uv run aizim research inbox add --project /absolute/path/to/project \
  --task-id addition --kind constraint --author 'Researcher name' \
  --body 'Keep the statement over natural numbers.'
uv run aizim research task pause --project /absolute/path/to/project --task-id addition
uv run aizim research task resume --project /absolute/path/to/project --task-id addition
uv run aizim research task resume --project /absolute/path/to/project --task-id addition \
  --max-rounds 9 --max-failures 4 --call-budget 800

uv run aizim research contribution add --project /absolute/path/to/project \
  --task-id addition --role problem_formulation --author 'Researcher name' \
  --body 'Selected the natural-number formulation and its intended scope.'
uv run aizim research contribution add --project /absolute/path/to/project \
  --task-id addition --role exposition --author 'Researcher name' \
  --body 'This lemma isolates the right identity law used in the next argument.'
uv run aizim research review add --project /absolute/path/to/project \
  --task-id addition --kind semantic_alignment --verdict accept \
  --author 'Reviewer name' --body 'The Lean statement matches the intended claim.'
```

Resuming clears the infrastructure-failure streak. A blocked target resumes only with limits that
allow another round; raise `--max-rounds` or `--max-failures` as needed. Limits apply to the
execution record only, so the target statement and its hash stay unchanged.

Contribution roles include problem formulation, definitions, strategy, counterexamples,
formalization, semantic review, library design, exposition, and source material. Review kinds
separate semantic alignment, research value, library quality, exposition, and release judgement.
Reviews bind to a target hash and the declaration evidence present at review time. Names are
operator-declared attribution, not authenticated identities. A local record and timestamp do not
establish originality or global first discovery. Consumed human guidance remains visible in each
attempt even when the configured participation mode is autonomous.

## Memory and offline search

```sh
uv run aizim research memory add --project /absolute/path/to/project \
  --task-id addition --kind dead_end --body 'Induction added unnecessary complexity.'
uv run aizim research memory search 'induction' --project /absolute/path/to/project --task-id addition
uv run aizim research search 'natural number identity' --project /absolute/path/to/project
uv run aizim research search 'addition_identity' --mode name --project /absolute/path/to/project
uv run aizim research search '?a + 0 = ?a' --mode type --project /absolute/path/to/project
```

The local declaration search reads project and installed package sources. Text search uses BM25;
name and type searches are heuristic discovery aids. Lean validates uses of retrieved declarations.
Recent round summaries, relevant scoped memory, current guidance, and matching local declarations
are supplied to the next attempt. Research memory is advisory, including entries labelled
conjecture or counterexample; verified declarations remain in the formal knowledge store.

## Dashboard and local research record

```sh
uv run aizim research dashboard --project /absolute/path/to/project --port 8765
uv run aizim research export --project /absolute/path/to/project --output research.html
uv run aizim research export --project /absolute/path/to/project --format json --output research.json
```

The dashboard binds to `127.0.0.1` and provides GET-only views. The HTML export is self-contained
and includes the target graph, Lean statements, round outcomes, human contributions, reviews,
memory, guidance, and measured usage. Exported files are private (`0600`) and existing files are
never overwritten. Export does not publish anything externally.

Optional `--prices prices.json` values are decimal strings per million tokens:

```json
{
  "YOUR_MODEL": {
    "input_per_million": "0",
    "cached_input_per_million": "0",
    "output_per_million": "0"
  }
}
```

Supply the rates applicable to your account; the example is a format illustration. Usage comes
from omp transport events, and arithmetic uses `Decimal`. Unmeasured stages and unknown prices
remain unknown. Known usage cost is a subtotal; a complete estimate is shown only when its
usage is complete. These estimates are not provider invoices. Providers that do not expose
compatible usage telemetry are shown as unmeasured.
