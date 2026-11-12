import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  mkdir,
  mkdtemp,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { verifyEvidenceDirectory } from "../../scripts/npm/verify-ci-evidence.mjs";
import {
  createCiEvidenceDocument,
  validateInstallSmokeEvidence,
} from "../../scripts/npm/write-ci-evidence.mjs";

const commit = "1".repeat(40);
const version = "0.1.0";
const targetRecords = {
  "darwin-arm64": { os: "darwin", arch: "arm64", libc: null },
  "linux-arm64": { os: "linux", arch: "arm64", libc: "glibc" },
  "linux-x64": { os: "linux", arch: "x64", libc: "glibc" },
};
const installChecks = {
  provider_free_local_install: true,
  provider_free_global_install: true,
  provider_free_version: true,
  provider_free_help: true,
  provider_free_doctor_fails_closed: true,
  no_agent_dependencies: true,
  python_315_bootstrap: true,
  cache_reused: true,
  uninstall_preserved_cache: true,
  research_task_roundtrip: true,
  bundled_agent_upgrade_preserved_state_credentials: true,
  missing_platform_exit_78: true,
  integrity_failure_exit_74: true,
};

function digest(algorithm, value) {
  return createHash(algorithm).update(value).digest(
    algorithm === "sha512" ? "base64" : "hex",
  );
}

function workflowJob(workflow, jobId) {
  const marker = `  ${jobId}:\n`;
  const start = workflow.indexOf(marker);
  assert.notEqual(start, -1, `missing workflow job: ${jobId}`);
  const remainder = workflow.slice(start + marker.length);
  const nextJob = remainder.search(/^  [a-z0-9_-]+:\n/mu);
  return nextJob === -1 ? remainder : remainder.slice(0, nextJob);
}

async function packageRecord(directory, name, filename, value) {
  await writeFile(join(directory, filename), value);
  return {
    name,
    filename,
    size: Buffer.byteLength(value),
    sha256: digest("sha256", value),
    integrity: `sha512-${digest("sha512", value)}`,
  };
}

async function writeDocument(path, document) {
  await writeFile(path, `${JSON.stringify(document, null, 2)}\n`);
}

async function readDocument(path) {
  return JSON.parse(await readFile(path, "utf8"));
}

async function evidenceFixture() {
  const root = await mkdtemp(join(tmpdir(), "aizim-ci-evidence-"));
  const paths = new Map();
  for (const [target, runner] of Object.entries(targetRecords)) {
    const directory = join(root, target);
    await mkdir(directory, { recursive: true });
    const platform = await packageRecord(
      directory,
      `@aiz.im/aizim-${target}`,
      `aiz.im-aizim-${target}-${version}.tgz`,
      `platform:${target}`,
    );
    const meta = await packageRecord(
      directory,
      "@aiz.im/aizim",
      `aiz.im-aizim-${version}.tgz`,
      "common-meta",
    );
    const path = join(directory, `native-evidence-${target}.json`);
    await writeDocument(path, {
      schema_version: 1,
      commit_sha: commit,
      version,
      target,
      dirty_tracked_inputs: false,
      restored_build_cache: false,
      runner,
      packages: [platform, meta],
      checks: {
        source_build: true,
        npm_test: true,
        ...installChecks,
      },
    });
    paths.set(target, path);
  }

  const minimumDirectory = join(root, "minimum-node");
  await mkdir(minimumDirectory, { recursive: true });
  const minimumPlatform = await packageRecord(
    minimumDirectory,
    "@aiz.im/aizim-linux-x64",
    `aiz.im-aizim-linux-x64-${version}.tgz`,
    "minimum-platform:linux-x64",
  );
  const minimumMeta = await packageRecord(
    minimumDirectory,
    "@aiz.im/aizim",
    `aiz.im-aizim-${version}.tgz`,
    "common-meta",
  );
  const minimumPath = join(minimumDirectory, "minimum-node-evidence.json");
  await writeDocument(minimumPath, {
    schema_version: 1,
    commit_sha: commit,
    version,
    target: "linux-x64",
    minimum_node_version: "v22.22.2",
    dirty_tracked_inputs: false,
    restored_build_cache: false,
    runner: targetRecords["linux-x64"],
    packages: [minimumPlatform, minimumMeta],
    checks: {
      source_build: true,
      ...installChecks,
    },
  });

  return {
    cleanup: async () => rm(root, { force: true, recursive: true }),
    minimumPath,
    output: join(root, "aggregate.json"),
    paths,
    root,
  };
}

async function verify(fixture) {
  return await verifyEvidenceDirectory({
    directory: fixture.root,
    commit,
    version,
    minimumNodeVersion: "v22.22.2",
    output: fixture.output,
  });
}

function installEvidenceDocument() {
  const packageHash = digest("sha256", "x");
  const integrity = `sha512-${digest("sha512", "x")}`;
  return {
    schema_version: 1,
    commit_sha: commit,
    target: "linux-x64",
    node_version: "v26.13.0",
    packages: [
      {
        name: "@aiz.im/aizim-linux-x64",
        filename: `aiz.im-aizim-linux-x64-${version}.tgz`,
        size: 1,
        sha256: packageHash,
        integrity,
      },
      {
        name: "@aiz.im/aizim",
        filename: `aiz.im-aizim-${version}.tgz`,
        size: 1,
        sha256: packageHash,
        integrity,
      },
    ],
    ready: {
      first: { sha256: packageHash, mtime_ns: "1" },
      second: { sha256: packageHash, mtime_ns: "1" },
      reused: true,
    },
    checks: { ...installChecks },
  };
}

test("requires every install observation before composing native evidence", async (context) => {
  for (const [name, mutate, pattern] of [
    [
      "failed provider-free doctor contract",
      (document) => {
        document.checks.provider_free_doctor_fails_closed = false;
      },
      /install checks/,
    ],
    [
      "changed ready marker",
      (document) => {
        document.ready.second.mtime_ns = "2";
      },
      /ready evidence/,
    ],
    [
      "missing provider-free global install",
      (document) => {
        delete document.checks.provider_free_global_install;
      },
      /install checks/,
    ],
    [
      "missing negative integrity test",
      (document) => {
        delete document.checks.integrity_failure_exit_74;
      },
      /install checks/,
    ],
    [
      "missing research task roundtrip smoke",
      (document) => {
        delete document.checks.research_task_roundtrip;
      },
      /install checks/,
    ],
    [
      "missing bundled-agent upgrade evidence",
      (document) => {
        delete document.checks.bundled_agent_upgrade_preserved_state_credentials;
      },
      /install checks/,
    ],
  ]) {
    await context.test(name, () => {
      const document = installEvidenceDocument();
      mutate(document);
      assert.throws(() => validateInstallSmokeEvidence(document), pattern);
    });
  }
});

test("composes native evidence only from matching build, test, pack, and install summaries", () => {
  const install = installEvidenceDocument();
  const packages = install.packages.map((record) => ({
    ...record,
    version,
  }));
  const document = createCiEvidenceDocument({
    target: "linux-x64",
    commit,
    version,
    build: {
      schema_version: 1,
      git_sha: commit,
      target: "linux-x64",
      dirty_tracked_inputs: false,
    },
    pack: {
      schema_version: 1,
      git_sha: commit,
      target: "linux-x64",
      packages,
    },
    install,
    testSummary: {
      schema_version: 1,
      commit_sha: commit,
      target: "linux-x64",
      checks: {
        node: true,
        rust: true,
        python: true,
        install_smoke: true,
        package_check: true,
      },
    },
    liveNodeVersion: "v26.13.0",
  });

  assert.deepEqual(document.runner, targetRecords["linux-x64"]);
  assert.equal(document.checks.source_build, true);
  assert.equal(document.checks.npm_test, true);
});

test("CI qualifies distribution, Lean, and deterministic omp contracts", async () => {
  const workflow = await readFile(
    new URL("../../.github/workflows/ci.yml", import.meta.url), "utf8",
  );
  const leanWorkflow = await readFile(
    new URL("../../.github/workflows/lean-integration.yml", import.meta.url), "utf8",
  );
  const ompWorkflow = await readFile(
    new URL("../../.github/workflows/omp-contract.yml", import.meta.url), "utf8",
  );
  const registryWorkflow = await readFile(
    new URL("../../.github/workflows/npm-registry-smoke.yml", import.meta.url), "utf8",
  );
  const releaseWorkflow = await readFile(
    new URL("../../.github/workflows/npm-release.yml", import.meta.url), "utf8",
  );
  for (const value of [
    "darwin-arm64", "linux-arm64", "linux-x64",
    "macos-15", "ubuntu-24.04-arm", "ubuntu-24.04",
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1",
    "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
    "fail-fast: false", "timeout-minutes: 45",
    'npm install --global --prefix "$RUNNER_TEMP/npm-12" npm@12.2.2',
    'echo "$RUNNER_TEMP/npm-12/bin" >> "$GITHUB_PATH"',
    "sudo apt-get install --yes ripgrep",
    'not lean_integration and not linux_sandbox and not macos_sandbox',
  ]) {
    assert.ok(workflow.includes(value), `missing CI contract: ${value}`);
  }
  const nativeJob = workflowJob(workflow, "native");
  assert.match(nativeJob, /sudo apt-get install --yes ripgrep/u);
  assert.match(nativeJob, /brew install ripgrep/u);
  assert.match(nativeJob, /rg --version/u);
  const minimumNodeJob = workflowJob(workflow, "minimum-node");
  for (const value of [
    'node-version-file: .node-version',
    'node-version: "22.22.2"',
    'npm install --global --prefix "$RUNNER_TEMP/npm-12-minimum" npm@12.2.2',
    'test "$(node --version)" = "v22.22.2"',
    '--minimum-node-version v22.22.2',
  ]) {
    assert.ok(minimumNodeJob.includes(value), `missing minimum Node CI contract: ${value}`);
  }
  for (const value of [
    "leanprover/lean-action@ba3fdfe5c4476c925da13bf11a005ddb47e47aee",
    "astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2",
    'version: "0.13.7"',
    "tests/integration/test_research_lean.py",
    "tests/integration/test_compiled_research_index.py",
    "tests/unit/test_lean_mcp_contract.py",
    "-m lean_integration",
  ]) {
    assert.ok(leanWorkflow.includes(value), `missing Lean CI contract: ${value}`);
  }
  const ompJob = workflowJob(ompWorkflow, "omp-contract");
  for (const value of [
    "darwin-arm64", "linux-arm64", "linux-x64",
    "macos-15", "ubuntu-24.04-arm", "ubuntu-24.04",
    'version: "0.13.7"', "uv sync --frozen",
    "tests/unit/test_omp_rpc.py",
    "tests/unit/test_omp_backend.py",
    "tests/integration/test_omp_workspace.py",
    "tests/unit/test_linux_sandbox_profile.py",
    "tests/unit/test_linux_profile_contract.py",
    "tests/unit/test_macos_profile_contract.py",
    "tests/unit/test_sandbox_profile.py",
    "tests/unit/test_probe_execution.py",
  ]) {
    assert.ok(ompJob.includes(value), `missing omp CI contract: ${value}`);
  }
  for (const target of [
    "- target: darwin-arm64", "- target: linux-arm64", "- target: linux-x64",
    "runner: macos-15", "runner: ubuntu-24.04-arm", "runner: ubuntu-24.04",
  ]) {
    assert.equal(
      ompJob.split("\n").filter((line) => line.trim() === target).length,
      1,
      `omp workflow must qualify ${target}`,
    );
  }
  const workflows = [workflow, leanWorkflow, ompWorkflow, registryWorkflow, releaseWorkflow];
  for (const document of workflows) {
    assert.doesNotMatch(document, /npm publish|NODE_AUTH_TOKEN|NPM_TOKEN|id-token:\s*write/u);
    assert.doesNotMatch(document, /@openai\/codex|@anthropic-ai\/claude-code|AIZIM_PROVIDER_CONTRACT/u);
    assert.match(document, /name: Create private temporary root/u);
    for (const line of document.match(/^\s*uses:\s*.+$/gmu) ?? []) {
      assert.match(line, /@[0-9a-f]{40}(?:\s+#\s+v\d+\.\d+\.\d+)?$/u);
    }
    assert.equal(
      document.match(/actions\/setup-node@/gu)?.length ?? 0,
      document.match(/package-manager-cache:\s*false/gu)?.length ?? 0,
    );
  }
  assert.doesNotMatch(ompWorkflow, /OPENAI_API_KEY|ANTHROPIC_API_KEY|secrets\./u);
  for (const document of [nativeJob, workflowJob(workflow, "minimum-node"), registryWorkflow, releaseWorkflow]) {
    assert.doesNotMatch(document, /leanprover\/lean-action/u);
  }
});

test("aggregates exactly three native targets and the minimum Node runtime", async () => {
  const fixture = await evidenceFixture();
  try {
    const aggregate = await verify(fixture);

    assert.deepEqual(aggregate.targets, Object.keys(targetRecords).sort());
    assert.equal(aggregate.commit_sha, commit);
    assert.equal(aggregate.version, version);
    assert.equal(aggregate.minimum_node_version, "v22.22.2");
    assert.equal(aggregate.platforms.length, 3);
    assert.equal(
      aggregate.meta.sha256,
      digest("sha256", "common-meta"),
    );
    assert.deepEqual(await readDocument(fixture.output), aggregate);
  } finally {
    await fixture.cleanup();
  }
});

test("rejects unknown fields, false checks, and target ABI mismatches", async (context) => {
  for (const [name, mutate, pattern] of [
    [
      "unknown field",
      (document) => {
        document.secret = "unexpected";
      },
      /evidence fields/,
    ],
    [
      "false check",
      (document) => {
        document.checks.provider_free_doctor_fails_closed = false;
      },
      /evidence checks/,
    ],
    [
      "Linux without glibc",
      (document) => {
        document.runner.libc = null;
      },
      /runner mismatch/,
    ],
  ]) {
    await context.test(name, async () => {
      const fixture = await evidenceFixture();
      try {
        const path = fixture.paths.get("linux-arm64");
        const document = await readDocument(path);
        mutate(document);
        await writeDocument(path, document);
        await assert.rejects(verify(fixture), pattern);
      } finally {
        await fixture.cleanup();
      }
    });
  }
});

test("rejects stale commits and duplicate or missing targets", async (context) => {
  await context.test("stale commit", async () => {
    const fixture = await evidenceFixture();
    try {
      const path = fixture.paths.get("darwin-arm64");
      const document = await readDocument(path);
      document.commit_sha = "2".repeat(40);
      await writeDocument(path, document);
      await assert.rejects(verify(fixture), /commit mismatch/);
    } finally {
      await fixture.cleanup();
    }
  });

  await context.test("duplicate target", async () => {
    const fixture = await evidenceFixture();
    try {
      const path = fixture.paths.get("linux-arm64");
      const document = await readDocument(path);
      document.target = "darwin-arm64";
      document.runner = targetRecords["darwin-arm64"];
      document.packages[0].name = "@aiz.im/aizim-darwin-arm64";
      await writeDocument(path, document);
      await assert.rejects(verify(fixture), /duplicate target/);
    } finally {
      await fixture.cleanup();
    }
  });
});

test("rejects modified tarballs, path traversal, and divergent meta bytes", async (context) => {
  await context.test("modified tarball", async () => {
    const fixture = await evidenceFixture();
    try {
      const document = await readDocument(
        fixture.paths.get("darwin-arm64"),
      );
      await writeFile(
        join(
          fixture.root,
          "darwin-arm64",
          document.packages[0].filename,
        ),
        "X".repeat(document.packages[0].size),
      );
      await assert.rejects(verify(fixture), /package digest mismatch/);
    } finally {
      await fixture.cleanup();
    }
  });

  await context.test("path traversal", async () => {
    const fixture = await evidenceFixture();
    try {
      const path = fixture.paths.get("darwin-arm64");
      const document = await readDocument(path);
      document.packages[0].filename = "../outside.tgz";
      await writeDocument(path, document);
      await assert.rejects(verify(fixture), /package filename/);
    } finally {
      await fixture.cleanup();
    }
  });

  await context.test("divergent meta bytes", async () => {
    const fixture = await evidenceFixture();
    try {
      const path = fixture.paths.get("darwin-arm64");
      const document = await readDocument(path);
      document.packages[1] = await packageRecord(
        join(fixture.root, "darwin-arm64"),
        "@aiz.im/aizim",
        `aiz.im-aizim-${version}.tgz`,
        "different-meta",
      );
      await writeDocument(path, document);
      await assert.rejects(verify(fixture), /meta package mismatch/);
    } finally {
      await fixture.cleanup();
    }
  });
});
