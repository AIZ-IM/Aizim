# Aizim authority boundary

Aizim owns research authority. One restricted omp RPC process performs one attempt; magpie owns
model routing and provider credentials. Neither omp output nor magpie telemetry can verify a
mathematical target. Only the trusted Lean publication path can do that.

## Trusted and untrusted processes

The trusted computing base includes the supervisor, StateService and its event store, the
capability issuer and gateway, the session broker, the materialized-view builder, platform sandbox
adapters, the pinned omp executable, and the local magpie service. Trusting the omp executable is
necessary: filesystem isolation alone does not constrain every network action of that process.
Model output, assistant summaries, RPC events, tool arguments, and transport digests are untrusted.

The supervisor redeems the gateway session and keeps the raw capability token and broker session ID
inside the trusted host. omp receives role-authorized host-tool definitions over stdio. Every call
passes JSON Schema validation at the RPC boundary and capability, operation, run, worker, lease,
expiry, and revocation checks in the gateway. Tool discovery does not confer authority. The host
never delegates generic event append, capability minting, document path resolution, Lean
verification, promotion, or environment approval to the model.

The gateway audits each refusal as `CapabilityDenied` and returns `CAPABILITY_DENIED`. Refusals
include run budget exhaustion and the per-attempt call budget (`WORKER_BUDGET_EXHAUSTED`). Each
attempt's trusted directive mints a `call_budget` from 1 through 10000 into its capability. The
gateway enforces that budget across all calls for the worker identity; worker arguments cannot
raise it. Historical capabilities without a minted budget retain the host worker limit.

Rate limiting retains the `CapabilityDenied` audit with reason `REQUEST_RATE_EXCEEDED`, but returns
the reserved `GATEWAY_RATE_LIMITED` result with `error.retry_after_seconds`, bounded from 0.001 to
60 seconds. Throttles do not count as refusal outcomes. The trusted omp host bridge waits for that
delay and retries the same call, checking authority again on each retry. Attempt deadlines and
cancellation still interrupt the wait, and throttled calls do not consume run or worker budgets.

When an authorized target raises, the gateway audits a `GatewayCallFailed` event and returns the
target's stable upper-case reason code as an ordinary tool error; other messages become
`TOOL_FAILED`, and exception text is never returned. The worker's channel stays usable after a
target error. Gateway-owned codes (`CAPABILITY_DENIED`, `GATEWAY_INTERNAL`, `GATEWAY_PROTOCOL`,
`GATEWAY_UNAVAILABLE`, and `GATEWAY_RATE_LIMITED`) are reserved; a target cannot claim them. Retry
metadata belongs only to gateway rate-limit results. Closed tool schemas reject worker-supplied
response codes and retry fields before dispatch, and the transport rejects malformed retry hints.

Autonomous attempts use `omp --mode rpc --no-ui --no-tools --no-extensions --no-skills --no-rules
--no-context-files`, an explicit cwd, and private per-attempt session storage. Built-in model shell
and file tools are disabled. Ambient extensions, skills, rules, context files, and MCP configuration
must not add another execution path. Host tools are the only model-directed mutation interface.
The trusted host gateway enforces the remaining logical authority boundary.

magpie owns provider accounts, credentials, model selection, routing, fallback, quotas, and provider
usage. Aizim records the observed omp version, executable identity, resolved model, and attempt
usage; it does not copy provider credentials into prompts, reports, or artifacts. Model selection
through `AIZIM_MODEL` and the former Codex/Claude Controller paths are removed.

Only a trusted `DeclarationPublished` event bound to the immutable target statement, candidate,
and attempt can mark a research target verified. Human semantic, value, exposition, and release
reviews remain independent records with operator-declared identity. A contribution ID match,
assistant summary, or successful RPC response alone is insufficient.

## Materialized view and filesystem isolation

`WorkspaceViewBuilder` accepts an explicit project-relative allowlist. It rejects traversal,
reserved `.git` and `.aizim` surfaces, symlinks, non-regular files, and multiply linked files. It
copies through descriptor-relative operations and checks source digests and metadata before
publication. Published files are fresh read-only copies; the manifest binds path, length, mode,
and SHA-256. The builder atomically allocates a mode-0700 parent under the system temporary
directory, which may be a shared sticky directory such as `/tmp`. View and scratch are separate
private siblings inside that parent. Closing the workspace, or failing during construction,
removes the private parent and its contents; the system temporary directory is never exposed
as a runtime read root.

Linux uses the system `bwrap` executable for filesystem isolation. The materialized view is
read-only and scratch is writable; the canonical project, state database, shared artifacts,
unleased files, and gateway Unix socket are hidden from the restricted process. Only declared
runtime roots are readable. The process receives a filtered environment. A missing or unusable
system bwrap or user namespace fails closed; there is no unsandboxed fallback. Aizim does not
use Codex's bundled bwrap or its permission-profile implementation.

macOS uses Apple's `/usr/bin/sandbox-exec` and the compiled Seatbelt profile to enforce the
filesystem boundary. It requires a canonical, executable omp file at the pinned version and
fails closed with a sandbox host error when that file is missing or invalid. The view is
read-only, scratch is writable, and canonical project/state and gateway socket access are
denied. The host kernel and platform sandbox mechanism are trusted.

## Network limitation

The host network is shared so the restricted omp process can reach local magpie. Consequently, omp
can access other local listeners and external networks reachable from the host. Linux bwrap
restricts the filesystem; it provides no destination deny rule, TCP destination confinement, or
network isolation guarantee in this configuration. Successful access to an arbitrary host TCP
listener is an expected allowed probe, not evidence of a network sandbox failure. Do not interpret
`SECURITY GATE PASS` as a promise that only magpie can be contacted. The new policy makes no
cross-platform TCP destination isolation claim and is not equivalent to the old denied-network
threat model.

The boundary therefore depends on the trusted pinned omp executable, disabled model shell/file
tools and ambient integrations, and the trusted host gateway. These controls restrict the model's
available actions; they do not establish OS enforcement of a TCP allowlist. A compromised trusted
executable, host service, kernel, or same-UID host process is outside this boundary.

## Capability and process lifecycle

The issuer persists only a SHA-256 token digest and its run, worker, role, lease, operation set,
and expiry. Raw tokens stay transient in the trusted broker/supervisor and never appear in omp
argv, environment, public reports, logs, or events. Mutable transport buffers are zeroed on
redemption, rejection, revocation, or closure; dropping immutable Python strings cannot guarantee
that their former memory is overwritten. All incomplete and exceptional paths revoke authority.

The broker creates `.aizim/run/gateway.sock` with mode `0600`. A private short socket alias may
address platform path-length limits; cleanup removes the alias. The restricted process must not
connect to the actual gateway Unix socket. The supervisor, rather than omp, redeems the session.

The supervisor bounds JSONL frames, correlates requests, reassembles negotiated protocol-v2
chunks, forwards cancellation, and terminates a non-cooperative child after timeout. Cleanup
revokes/releases attempt resources and kills/reaps process-group descendants before propagating
cancellation. An old omp session or final message never restores authority after restart.

## No-model security gate

After initialization, run:

```console
aizim security-probe --project LEAN_PROJECT
```

The gate starts the real pinned omp under the restricted OS profile, performs RPC `get_state` and
tool-inventory inspection, and closes it without sending a prompt or making a model request. It
also runs gateway authority checks and filesystem/environment probes. Required controls include
six filesystem denials, denial of gateway Unix-socket access, denial of secret-environment access,
read access to the allowed view, write access to scratch, and an allowed host TCP connection. The
report states the shared-network limitation explicitly. Passing RPC startup does not by itself
qualify the gateway or filesystem controls.

Protected file hashes and logical projections must remain unchanged. The gate closes brokers,
listeners, views, scratch directories, and state handles, restarts StateService, and requires replay
to reproduce its evidence. Denials carry fixed redacted operation/reason codes. A single attack
probe policy hash binds that probe's records and terminal receipt; RPC attestation uses separate
paths and does not establish an identical policy hash.

The completion operation is `omp_security_gate_complete`. Old Codex `gate_b_complete` receipts
and their historical passing fixtures are legacy evidence only. They remain replayable and keep
their original execution identities, but cannot qualify an omp security gate. Probe reports use
`agent_version` for the observed omp version.

An unsupported omp version, absent sandbox mechanism, failed RPC inspection, unexpected protected
access, skipped or inconclusive required control, digest mismatch, or replay failure blocks
execution. Platform qualification requires the real gate on that host and architecture. A pass
covers the recorded controls and stated trust assumptions; it does not establish formal theorem
verification or OS TCP isolation.
