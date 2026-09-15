# Aizim authority boundary

Slices 1 and 2 admit an agent only after two independent controls agree: the
capability gateway authorizes the requested logical operation, and the
platform sandbox profile permits the operating-system action. Passing either control
alone grants no authority.

## Trusted and untrusted processes

The trusted computing base contains the Aizim CLI security-gate orchestrator,
`StateService` and its single SQLite event store, the capability issuer and
gateway, the gateway session broker, the materialized-view builder, the platform
sandbox adapters, and the launcher-owned Codex parent process. These components
may read canonical project state and append validated audit events.

Agents, model-controlled commands, and the gateway sidecar are untrusted. The
sidecar receives one role-bound capability after peer authentication; it is not
a general shell or filesystem mutation service. Trusted-only operations such as
state append, capability minting, document path resolution, Lean build or
verification, promotion, and environment approval are absent from every agent
role capability set.

Codex model transport occurs in the launcher-owned parent. Commands selected by
the model run under the compiled profile with network disabled, so allowing the
parent to reach model infrastructure does not give the model-controlled shell a
network path.

The foreground controller is trusted orchestration. The attested native Codex CLI
is a trusted model transport, as it is for Workers; model outputs and model-selected
tools are untrusted. Codex API traffic uses the client's own authenticated network
connection. Its tools use a separate native permission profile: only the empty
view and scratch are available, the canonical project and copied authentication
directory are denied, shell environment inheritance and web search are disabled,
and tool network access is disabled. Nesting the whole Codex CLI inside its own
sandbox is unsupported by the pinned CLI's filesystem helpers.

Claude keeps the outer OS filesystem sandbox, disabled built-in tools and MCP,
and an active network proxy restricted to its provider domains. Provider auth
files are copied into private temporary storage; user settings and history are
not copied. The model receives bounded assignment context and returns one validated
dispatch, blocked, or reject decision. A validated dispatch may name only the assigned worker and fixed
budget and timeout ceilings. Trusted code persists only the validated directive
instruction and fixed hashes; prompts, raw provider envelopes, responses,
credentials, authenticated URLs, and full environments are not durable state.

The supervisor owns the sole live `StateService`. Same-UID CLI clients may use
the public configure, register, assign, and typed research mutations; generic event
append remains service-session-only. The selected controller provider never
selects the worker backend: worker execution remains Codex-backed and passes
through the existing lease, capability, gateway, sandbox, cleanup, and Lean
verification boundaries. Terminal assignment IDs are durable and never
dispatched again after restart.

Research records add immutable mathematical targets, dependency scheduling, bounded
attempts, scoped advisory memory, and operator contributions/reviews. Only existing
Lean publication evidence can mark a target verified. Human acceptance of meaning,
value, or exposition never substitutes for formal verification. Model API output
uses a provider-compatible flat schema and is normalized into the stricter local
decision union; unknown fields and invalid action-specific combinations remain errors.

See [Codex permission scope](https://learn.chatgpt.com/docs/permissions#scope-and-enforcement)
for the distinction between client service traffic and sandboxed command traffic.

Launcher-owned host checks also start in a fresh process group. Whether their
direct parent exits successfully or with an error, Aizim returns its captured
status and output only after killing and reaping any same-group descendants,
including descendants that detached from standard input and output. Cleanup is
completed before cancellation is propagated.

## Materialized view and platform sandbox profile

`WorkspaceViewBuilder` accepts an explicit project-relative allowlist. It opens
the canonical root once, rejects traversal, reserved `.git` and `.aizim`
surfaces, symlinks, non-regular files, and multiply linked files, and copies
bytes through descriptor-relative operations. Each source digest and metadata
snapshot is checked again before publication. Published files are fresh `0444`
copies, directories are read-only, and the manifest binds path, length, mode,
and SHA-256. Scratch is a separate private ephemeral directory.

Worker final messages use a parent-owned temporary directory under `.aizim/run`,
outside the model's writable scratch and readable view. The CLI parent can write
there as a normal user, while model tools remain denied access. The directory is
removed after result parsing and on cancellation.

`MacOSSandboxAdapter` compiles and then validates the exact inline Codex
permission profile before launching `codex sandbox --permission-profile`.
Seatbelt denies the canonical project and `.aizim`, permits read-only access to
the view and developer runtime, permits writes only in scratch, removes the
parent environment, and disables child network access. The production attack
probe exercises six filesystem denials, two socket denials, one environment
denial, and two allowed controls. A synthetic environment value verifies that
shell isolation is real without using user credentials.

`LinuxSandboxAdapter` enforces the same permission contract through the
package-local Codex CLI and its exact bundled `codex-resources/bwrap`. It
accepts only Linux, Codex `0.154.0`, canonical regular executable images, and
a working user-namespace/bwrap probe. Missing or unusable bwrap fails before a
worker starts; there is no Landlock or unsandboxed fallback. The Linux profile
uses a minimal fixed `PATH`, reads only the materialized view and declared
runtime roots, writes only scratch, denies the canonical project, and disables
child network access.

## Capability and socket lifecycle

The issuer persists only a SHA-256 token digest plus the run, worker, role,
lease, operation set, and expiry. The raw token is held transiently by the
trusted broker and is never rendered in argv, environment, logs, events, or
public report objects. Broker and transport bytearrays are zeroed when their
one-shot session is redeemed, rejected, revoked, or closed. Immutable Python
string references are dropped after revocation and closure, but Python does not
guarantee that their former memory is overwritten. Call-time checks bind every
operation to the persisted role and claims; discovery does not replace
authorization.

The authority helper carries the persisted token digest with its evidence and
revokes it on every exceptional or incomplete-evidence path. Once that helper
returns, the live gate installs its cleanup boundary before reading protected
digests or constructing the broker; digest, hash, broker, startup, and sandbox
failures therefore all close any constructed broker and revoke the capability.

The broker creates the actual socket entry at the canonical project location
`.aizim/run/gateway.sock` with mode `0600`. When an absolute project path cannot
fit Darwin's Unix-socket address, the gate reaches that same canonical directory
entry through a private mode-`0700` short alias and removes the alias afterward.
The sandbox must make zero accepted connections to this socket and to a
non-allowlisted loopback listener.

Authorization failures append fixed, redacted `CapabilityDenied` events.
Sandbox results append `SandboxProbeDenied` or `SandboxProbePassed` events with
fixed operation and reason codes. The gate takes its protected logical digest
after capability setup, verifies protected file hashes and that digest after all
attacks, closes every broker, listener, view, scratch directory, and state
handle, restarts `StateService`, and requires replay to reproduce the denial
evidence without changing protected projections.

## Threat assumptions and platform status

Darwin peer audit tokens authenticate the executable image of the Python
interpreter that runs the console script; they do not authenticate an individual
Python module. Slices 1 and 2 therefore trust unsandboxed processes running as
the same Unix user. Removing that same-UID host-process assumption requires a
native launcher with a separately authenticated image. The controls also assume
the host kernel, Apple Seatbelt implementation, pinned Codex CLI image, and Aizim
trusted process are not compromised.

Linux support is glibc-only and becomes qualified per architecture only after
the real attack probe passes in the native CI job. An absent platform sandbox mechanism, a Codex
version mismatch, a skipped or inconclusive attack, an unexpected allow, or a
replay or digest mismatch fails closed.

Run the credential-free gate only after initialization:

```console
aizim security-probe --project LEAN_PROJECT --backend codex --no-model
```

`--no-model` prevents a model API request; it does not select a weaker sandbox.
Success is the fixed nine-line `SECURITY GATE PASS` summary. Every readiness,
version, sandbox, authorization, digest, or replay failure exits with status 3
and prints only `SECURITY GATE FAIL`.
