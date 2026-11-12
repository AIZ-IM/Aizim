# Aizim event recovery runbook

Recovery is event-led and fail-closed. Preserve the append-only audit history and registered
artifacts; never manufacture a success event or edit a projection to make status look healthy.

Current attempts use omp with the local magpie gateway. Preserve historical Codex/Claude events,
exports, and execution identities unchanged; legacy evidence remains replayable but does not
qualify a current omp security gate or acceptance run.

## Non-negotiable rules

- Never open or edit `.aizim/state.sqlite3` manually during a live run. Use `aizim status`, the
  StateService RPC, and registered artifacts.
- A failed or skipped required security control prohibits autonomous execution. Re-establish
  readiness and the current omp `SECURITY GATE PASS` before starting another attempt.
- Do not reuse a worker capability, expired lease, stale patch, or old environment fingerprint.
- Do not delete a quarantined contribution or partially written evidence while investigating it.

## First response

1. Stop scheduling new work and preserve stderr/stdout from the failed command.
2. Capture state through the supported interface:

   ```sh
   umask 077
   AIZIM_STATUS_FILE="$(mktemp "${TMPDIR:-/tmp}/aizim-status.XXXXXX")"
   uv run aizim status --project PROJECT --json > "$AIZIM_STATUS_FILE"
   printf '%s\n' "$AIZIM_STATUS_FILE"
   ```

3. Record the project commit, tool pins, run ID, knowledge epoch, worker states, lease states,
   candidate states, runtime events, and artifact directory.
4. If `.aizim/run/state.sock` is live, continue using only the CLI/RPC. Do not start a second
   StateService or inspect SQLite directly.
5. After all run processes stop, verify replay before rescheduling.

Record the observed omp version and executable hash, resolved magpie model, attempt ID, and
untrusted transport digest. Keep raw provider credentials, capability tokens, broker session IDs,
and complete environments out of incident reports. A magpie fallback or usage record never proves
publication or completion.

## Worker crash or timeout

The supervisor aborts a timed-out or cancelled omp prompt, terminates a non-cooperative child,
revokes its capability, and releases attempt resources. `WorkerCrashed` or `WorkerTimedOut`,
`WorkerStopped`, and `LeaseReleased` provide lifecycle evidence where those events apply. Research
attempt records must show interruption/failure rather than infer success from an assistant message.

- Restart through `aizim research run --project PROJECT` so the trusted supervisor recovers
  already-published evidence before scheduling. It must never repeat a terminal assignment or
  turn an old omp session into a new attempt.
- New work receives fresh attempt/run/worker identities, lease, host session, and capability.
- If a lease remains live after expiry, let the broker record `LeaseRecovered` before scheduling.
- Never edit the leased file, revive the old gateway token, or send a model request to probe
  whether an interrupted prompt had finished.

## Shared Lean runtime crash

The shared bridge records `LeanRuntimeCrashed` and starts a replacement process with
`LeanRuntimeRestarted`. A restarted runtime must reconstruct the worker document from broker-owned
state and return the same goal before work continues.

- Do not publish a result obtained only before the crash.
- Require fresh diagnostics, whole-project build, source scan, and axiom verification from the
  restarted runtime.
- If restart or the reconstructed goal fails, abort the run; do not downgrade it to a warning.

## Expired lease

An expired lease is authority loss, even when its file still exists.

1. Confirm the current lease projection and formal epoch pair with `aizim status --json`.
2. Let the broker recover the lease; positive evidence is `LeaseRecovered`.
3. Reschedule with a new lease and capability.
4. Re-verify any queued immutable snapshot without relying on the expired worker process.

## Stale patch and snapshot after later edits

- A patch contribution is stale when its expected document version no longer matches. Keep the
  original contribution immutable and replay/rebase it into a new contribution against the current
  document and epoch pair.
- An immutable snapshot remains directly promotable after later edits to the worker document when
  its payload hash, formal dependencies, environment fingerprint, and epoch pair are unchanged.
- If the base or knowledge epoch changed, rebase and re-verify either payload form. Never rewrite
  the old contribution in place.

## Interrupted publication

Publication states move only forward:

```text
queued -> staged -> verified -> materialized -> published
                 \-> quarantined
```

Use the latest durable state:

| State at interruption | Recovery action |
|---|---|
| `queued` | A new owner may claim it in deterministic enqueue order. |
| `staged` | After the claim expires, recover to `staged` and run every verification again. |
| `verified` | Recover the claim, restage, and re-verify before materialization. |
| `materialized` | Hash-check the materialized module, restage, and re-verify before atomic publication; quarantine a mismatch. |
| `published` | Treat publication as idempotent; verify the declaration and delta already exist and do not append a second publication. |
| `quarantined` | Inspect the failure and artifact hash. Do not promote the same queue entry; submit a new contribution only after correcting the cause. |

No recovery may skip a state, move backward by editing history, or infer success from a file that
lacks the matching event and hash.

## Replay verification

For a completed omp/magpie foundation package, the acceptance checker verifies StateService RPC
replay, event/epoch links, the trace chain, and all five registered hashes. Ordinary research runs
need not produce this specific package; inspect their bound attempt/publication records instead.

```sh
uv run python scripts/check_foundation.py --project PROJECT --run-id RUN_ID
```

For a non-acceptance incident, wait until the live service has stopped, then open state only through
`StateService` and require `matched=True`:

```sh
uv run python -c 'from pathlib import Path; from aizim.state import StateService, StateServiceConfig; p=Path("PROJECT"); s=StateService(StateServiceConfig(p,"recovery-audit")); r=s.replay_verify(); print({"matched":r.matched,"logical_digest":r.logical_digest}); s.close(); raise SystemExit(0 if r.matched else 1)'
```

A replay mismatch blocks rescheduling. Preserve the project and escalate; do not repair the
database manually.

`latest-real` selects omp/magpie evidence only. Historical Codex events and artifacts retain their
original identities and can be audited through StateService replay, but cannot produce a new
passing omp acceptance receipt. Do not rewrite an old manifest, rehash a historical artifact, or
rename `gate_b_complete` to `omp_security_gate_complete` to make it eligible.

## Quarantine inspection

Read candidate projections without mutating them:

```sh
uv run aizim status --project PROJECT --json \
  | jq '.candidates[] | select(.state.payload.state == "quarantined")'
```

Correlate the contribution ID with `PromotionFailed`, its reason code, and artifact hash through
the StateService event query or the completed formal trace. Verify the referenced artifact with
`shasum -a 256`; never execute or import quarantined content during inspection.

## Return-to-service gate

Resume through `aizim research run` only when replay matches, no expired lease remains active,
interrupted publication is recovered or quarantined, and doctor and the current no-model security
gate pass. The gate performs real restricted omp RPC `get_state` and tool-inventory inspection
without a prompt/model call, plus gateway authority and filesystem/environment probes. Its terminal
operation must be `omp_security_gate_complete`; legacy Gate B evidence cannot satisfy it.

The view is read-only, scratch is writable, and canonical project/state and the gateway Unix socket
are restricted. Linux uses system bwrap filesystem isolation with the host network shared to reach
local magpie; omp can therefore reach other local listeners and external networks available to the
host. The report must acknowledge that there is no OS TCP destination deny or isolation guarantee.
Trusted pinned omp, disabled model shell/file tools and ambient integrations, and the trusted host
gateway enforce the remaining authority. Do not treat an allowed host TCP connection as a recovery
failure or a pass as proof of network confinement.

A completed foundation acceptance package additionally requires `FOUNDATION ACCEPTANCE PASS
16/16`. That result does not replace bound Lean publication evidence for a research target or human
semantic review.
