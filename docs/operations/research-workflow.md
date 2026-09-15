# Research workflow

Research targets connect an immutable Lean statement to a bounded execution loop, dependencies,
research memory, human guidance, and verification evidence. The foreground process owns the same
`StateService`, capability gateway, document leases, and promotion service as the original smoke
workflow. `lakefile.toml` and `lakefile.lean` projects are supported. The supervisor adds an
`AizimResearch` library inside its run copy; source project files remain the frozen input.

Prepare the pinned Lean and Codex toolchains described in the foundation runbook. Install project
dependencies with Lake before initialization when the project uses external packages.

Official npm Codex installations are resolved to their native executable for version and image
attestation. Codex's API client uses its own network connection; its model-selected tools use the
restricted native permission profile. Claude's transport uses an outer filesystem sandbox and a
provider-domain proxy. The authority-boundary document describes these distinct controls.

```sh
uv run aizim init /absolute/path/to/project
uv run aizim controller configure --project /absolute/path/to/project \
  --provider codex --model gpt-6-astra

uv run aizim research task add --project /absolute/path/to/project \
  --task-id addition --title 'Addition identity' \
  --statement '(n : Nat) : n + 0 = n' --import Std \
  --author 'Researcher name' --source-ref 'local:research-notes' \
  --max-rounds 6 --max-failures 3 --timeout-seconds 300

AIZIM_MODEL=gpt-6-astra uv run aizim research run \
  --project /absolute/path/to/project --max-seconds 1800
```

The statement contains the proposition, or binders followed by its result type, and no proof.
New dependency targets are added after their dependencies using repeated `--depends-on TASK_ID`.
Independent ready targets run concurrently, bounded by `resources.max_proof_workers` and
`resources.scratch_slots` (1–64). One shared Lean runtime remains serialized for formal operations.
`research run --watch` accepts later tasks and guidance without exiting when the frontier is idle.
`controller start --foreground` selects this research loop when research targets exist.

The original `worker assign` interface remains a one-assignment execution interface. Its formal
tasks use `--task 'prove LEAN_STATEMENT'`; free-form tasks are rejected instead of being replaced
with `True`. Use research targets for dependencies, repair rounds, and durable research context.

## Verification and recovery

Before real research execution, the no-model security gate must pass. A worker submission is not
a verified result: only a `DeclarationPublished` event from the existing Lean promotion path can
settle a target as verified. An advisory controller response, worker summary, or human review
cannot do so. Failed rounds retain feedback and start another bounded attempt. Restart recovery
settles already-published work without repeating it, and interrupts unfinished attempts before
continuing. Completed target statements, imports, dependencies, and source references are immutable.
Create another target when changing the mathematical question.

The default run exits once no eligible work remains. Exit code `5` means targets remain pending,
blocked, paused, or running. SIGINT/SIGTERM cancel active work and retain the research record.
Changing the primary controller configuration requires restarting the active research loop.

## Human guidance and contributions

Guidance is read at round boundaries. A pause takes effect after the active attempt settles;
SIGINT/SIGTERM are the immediate run cancellation controls.

```sh
uv run aizim research inbox add --project /absolute/path/to/project \
  --task-id addition --kind constraint --author 'Researcher name' \
  --body 'Keep the statement over natural numbers.'
uv run aizim research task pause --project /absolute/path/to/project --task-id addition
uv run aizim research task resume --project /absolute/path/to/project --task-id addition

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
from CLI transport events, and arithmetic uses `Decimal`. Unmeasured stages and unknown prices
remain unknown. Known usage cost is a subtotal; a complete estimate is shown only when its
usage is complete. These estimates are not provider invoices. Providers that do not expose
compatible usage telemetry are shown as unmeasured.
