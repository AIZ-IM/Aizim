# lean-host-mcp 0.14.2 research

Recorded 2026-11-08. The complete requests, JSON-RPC responses, tool schemas, fixture sources, build output, and per-call file comparisons are in [the evidence](2026-11-08-lean-host-mcp-evidence.json). The reproducible disposable-project probe is [research_lean_host_mcp.py](../../scripts/qa/research_lean_host_mcp.py). This is an evaluation, not a production integration or a substitute for Lake verification.

## Decision

Keep the existing Lean/Lake verification authority and current LSP integration. `lean-host-mcp` is promising for an optional future research adapter: it provides in-memory trials, per-declaration verification, source digests and explicit artifact freshness. It has a different tool surface and result contract, requires a locally compiled worker for each Lean toolchain, and the sampled success verdict alone does not enforce an axiom policy. Adopting it requires an explicit adapter, separate policy and failure handling, rather than switching the existing MCP executable.

## Installed versions and isolation

- Host: `lean-host-mcp 0.14.2`, the latest stable version in [crates.io metadata](https://crates.io/crates/lean-host-mcp) at the time of the study. Its published crate declares Rust 1.94 minimum. Its corresponding worker is also 0.14.2, linked against Lean 4.35.0.
- Lean: `Lean 4.35.0`, commit `eb6cafb7290e8bd5f2937522d8d804f58f02bafc`, `/root/.elan/toolchains/leanprover--lean4---v4.35.0`. This existing installation was not changed.
- Rust: `rustc 1.99.1 (58cf8f9c3 2026-10-13)`, isolated under `/root/.cache/aizim-lean-host-mcp/rustup` and `/root/.cache/aizim-lean-host-mcp/cargo`.
- Host executable: `/root/.cache/aizim-lean-host-mcp/host/bin/lean-host-mcp`. Worker: `/root/.cache/aizim-lean-host-mcp/data/lean-host-mcp/workers/v4.35.0/lean-host-mcp-worker`. `install-worker --list` reported `supported`, `fresh`, `runs`, `compatible`; the installer ran its own `Nat.add_zero` smoke test.
- LSP comparison: isolated `lean-lsp-mcp 0.33.0`, `leanclient 0.13.2`, `mcp 2.2.0`, Python 3.15.2; server initialize advertised `Lean LSP` 0.33.0. No project dependency configuration was changed.

The host parent advertises five tools: `lean_context`, `lean_lookup`, `lean_status`, `lean_trial`, `lean_verify`. The upstream default-branch documentation presently describes an additional `lean_runtime` tool, but the installed 0.14.2 did not advertise it; an exploratory request returned JSON-RPC `-32602 tool not found`. Do not infer a release's capabilities from the evolving default branch; enumerate the installed server. See [upstream](https://github.com/jcreinhold/lean-host-mcp), [tool catalog](https://github.com/jcreinhold/lean-host-mcp/blob/main/docs/tool-catalog.md), and [worker operations](https://github.com/jcreinhold/lean-host-mcp/blob/main/docs/operations.md#installing-workers).

## Observations

The fixture is an Init-only Lake project built before calls and rebuilt after edits; both builds exited 0. Calls used the actual stdio JSON-RPC MCP handshake, with complete results in the evidence. The project's own sources were intentionally edited only between calls by the probe.

| Case | Observed result | Implication |
| --- | --- | --- |
| `Probe.good` | `verified`; axioms `["propext"]` | Standard Lean axioms may occur even in a valid proof; decide what is acceptable explicitly. |
| `Probe.sorryProof`, `allow_sorry=false` | `has_sorry`, `contains_sorry=true`, axioms `["sorryAx"]` | Reject on the verdict and facts, not transport success. |
| Same declaration, `allow_sorry=true` | `verified`, still `contains_sorry=true` and `["sorryAx"]` | `verified` is conditional on the requested policy. `contains_sorry_ax` was false even though the reported axiom list contained `sorryAx`; inspect the full facts and axiom list. |
| `Probe.axiomProof` | `verified`, axioms `["Probe.assumed"]` | Successful verification does not imply freedom from user-defined axioms. |
| Explicit target `Probe.assumed` | `not_found`, `axioms_available=false` | In this fixture host verification did not resolve the axiom declaration as a target; an empty axiom list here is not proof of absence. A warning says the axiom set could not be computed. |
| Illegal `Probe.bad` | `has_unresolved_goals`; error diagnostic `True.intro` has type `True` but `False` expected; axiom list included `sorryAx` | A rejected Lean declaration is a semantic result, not necessarily an RPC error. Never treat its emitted axiom list as a verified theorem. |
| Trial tactics `simp`, `exact True.intro`, `sorry` | `closed`, `failed`, `closed` respectively | A closed tactic trial is not a proof acceptance verdict: `sorry` also closed it. Re-verify the intended declaration with policy. |
| Missing declaration | `not_found` | A normal semantic negative result. |

The LSP server's diagnostics reported a `sorry` warning and the invalid declaration's type error. Its `lean_verify` classified `Probe.good` as `standard` with `propext`, `Probe.sorryProof` as `incomplete` with `sorryAx`, and both the custom axiom and its dependent theorem as `custom` with `Probe.assumed`. This is a small behavioral comparison, not a benchmark or equivalence proof; LSP was queried after the rebuild. It has 22 advertised tools and a different request/response contract.

## Source and artifact freshness

Initially project-scope semantic references carried `.ilean` `build_fresh`. The probe changed `Probe.Base.base` from 1 to 2 and appended a new theorem without building. `lean_verify` found the new theorem and returned source `edit_fresh` with a SHA-256 matching the edited bytes. Project references still returned results but marked `.ilean` `stale_build` and warned that they reflect the previous build. With explicit imports, `#eval Probe.base` returned **1** before rebuild; a source-overlay command against the edited `Probe/Base.lean` returned **2**. After `lake build`, the same explicit import returned **2**, and references returned `build_fresh` again. Thus an edit-fresh target source fact does not establish that its imports or project index reflect unbuilt dependency edits. Coordinate build and source revision if an exact snapshot matters.

## Read-only observations and failure channels

For every recorded call, the probe compared SHA-256 of every project file except `.log` and `.lock` immediately before and after; it observed no changed project files for either server. This does not prove sandboxing or absence of writes outside the project. Builds and the probe's deliberate source edits are outside those per-call windows; server logs and caches live outside the project. The LSP verification tool's advertised annotation says `readOnlyHint=false` and `destructiveHint=true`; the observed sample does not override that contract.

Failure classification observed on host 0.14.2:

- Lean errors and negative verification outcomes appear in `data` with successful JSON-RPC and `isError=false`.
- An unsupported status kind returned `data: null` and a non-retryable `invalid_kind` issue in `errors`, also with successful JSON-RPC.
- A missing source file, nonexistent project, or intentionally missing worker produced JSON-RPC `-32603`; the missing-worker message named the toolchain and `install-worker` recovery command.
- The host documentation describes retryable worker/runtime issues in `errors`; crash, timeout, restart exhaustion and concurrency pressure were **not exercised**. Do not classify them based on the three configuration/I/O samples above.

## Reproduce

The following Linux x86-64 commands install to an isolated research root and require network access, `uv`, and the existing elan-managed Lean toolchain. `install-worker` discovers toolchains under `$HOME/.elan/toolchains`; `LEAN_SYSROOT` supplies the matching headers. `XDG_DATA_HOME` controls both worker installation and runtime lookup. The build may download substantial Rust dependencies. Choose a different root if desired; keep it consistent with the probe.

```sh
export STUDY_ROOT="$HOME/.cache/aizim-lean-host-mcp"
export CARGO_HOME="$STUDY_ROOT/cargo" RUSTUP_HOME="$STUDY_ROOT/rustup"
export XDG_DATA_HOME="$STUDY_ROOT/data" XDG_CACHE_HOME="$STUDY_ROOT/cache"
export CARGO_TARGET_DIR="$STUDY_ROOT/target" CARGO_BUILD_JOBS=2
export LEAN_HOST_MCP_CONFIG_DIR="$STUDY_ROOT/config"
export LEAN_SYSROOT="$HOME/.elan/toolchains/leanprover--lean4---v4.35.0"
export PATH="$CARGO_HOME/bin:$HOME/.elan/bin:$PATH"
unset LEAN_HOST_MCP_WORKERS_DIR
mkdir -p "$STUDY_ROOT"
curl -fsSL https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init -o "$STUDY_ROOT/rustup-init"
chmod +x "$STUDY_ROOT/rustup-init"
"$STUDY_ROOT/rustup-init" -y --no-modify-path --profile minimal --default-toolchain 1.99.1
cargo install lean-host-mcp --version 0.14.2 --locked --root "$STUDY_ROOT/host"
"$STUDY_ROOT/host/bin/lean-host-mcp" install-worker --toolchain v4.35.0
"$STUDY_ROOT/host/bin/lean-host-mcp" install-worker --list
export UV_CACHE_DIR="$STUDY_ROOT/uv-cache" UV_TOOL_DIR="$STUDY_ROOT/uv-tools" UV_TOOL_BIN_DIR="$STUDY_ROOT/uv-bin"
uv tool install --python 3.15.2 lean-lsp-mcp==0.33.0
python scripts/qa/research_lean_host_mcp.py --install-root "$STUDY_ROOT" --output /tmp/lean-host-evidence.json
```

The script records the full response and stderr logs beside the output, deletes its temporary Lake project on exit, and saves partial evidence if it aborts. Omit the LSP installation to record an unavailable comparator; no network service, production source or shared dependency file is modified by the probe itself. Toolchains and package resolution may evolve; pinning above reproduces the observed versions rather than asserting they remain the latest.

Unverified: Mathlib-scale memory/performance and semantic search, concurrent project behavior, remote/HTTP transport, worker crash recovery, cancellation, timeout and restart handling, untrusted project isolation, transitive dependency freshness under concurrent builds, non-Linux platforms, and production Aizim integration. Upstream notes worker imports and large projects can consume substantial memory; size and test on the intended workload before use.
