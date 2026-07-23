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
  "darwin-x64": { os: "darwin", arch: "x64", libc: null },
  "linux-arm64": { os: "linux", arch: "arm64", libc: "glibc" },
  "linux-x64": { os: "linux", arch: "x64", libc: "glibc" },
};
const installChecks = {
  local_install: true,
  global_install: true,
  npx_no_install: true,
  python_312_bootstrap: true,
  cache_reused: true,
  uninstall_preserved_cache: true,
  local_codex_01450: true,
  global_codex_01450: true,
  ready: true,
  security_gate: true,
  aizim_run: true,
  missing_platform_exit_78: true,
  integrity_failure_exit_74: true,
};

function digest(algorithm, value) {
  return createHash(algorithm).update(value).digest(
    algorithm === "sha512" ? "base64" : "hex",
  );
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
    minimum_node_version: "v22.14.0",
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
    minimumNodeVersion: "v22.14.0",
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
    node_version: "v26.5.0",
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
      "failed security gate",
      (document) => {
        document.checks.security_gate = false;
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
      "missing global Codex resolution",
      (document) => {
        delete document.checks.global_codex_01450;
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
    liveNodeVersion: "v26.5.0",
  });

  assert.deepEqual(document.runner, targetRecords["linux-x64"]);
  assert.equal(document.checks.source_build, true);
  assert.equal(document.checks.npm_test, true);
});

test("CI pins current actions and qualifies the exact four native runners without publishing", async () => {
  const workflow = await readFile(
    new URL("../../.github/workflows/ci.yml", import.meta.url),
    "utf8",
  );
  const leanWorkflow = await readFile(
    new URL("../../.github/workflows/lean-integration.yml", import.meta.url),
    "utf8",
  );
  for (const value of [
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
    "macos-15",
    "macos-15-intel",
    "ubuntu-24.04-arm",
    "ubuntu-24.04",
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020",
    "leanprover/lean-action@38fbc41a8c28c4cbaec22d7f7de508ec2e7c0dd9",
    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "fail-fast: false",
    "timeout-minutes: 45",
  ]) {
    assert.ok(workflow.includes(value), `missing CI contract: ${value}`);
  }
  assert.doesNotMatch(
    workflow,
    /npm publish|NODE_AUTH_TOKEN|NPM_TOKEN|id-token:\s*write/u,
  );
  for (const value of [
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020",
    "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9",
    "leanprover/lean-action@38fbc41a8c28c4cbaec22d7f7de508ec2e7c0dd9",
    'version: "0.11.31"',
    "npm@12.0.1",
    "@openai/codex@0.145.0",
  ]) {
    assert.ok(leanWorkflow.includes(value), `missing Lean CI contract: ${value}`);
  }
  assert.doesNotMatch(
    `${workflow}\n${leanWorkflow}`,
    /0\.11\.29|0\.144\.6|NODE_AUTH_TOKEN|NPM_TOKEN|id-token:\s*write/u,
  );
  for (const line of `${workflow}\n${leanWorkflow}`.match(/^\s*uses:\s*.+$/gmu) ?? []) {
    assert.match(line, /@[0-9a-f]{40}(?:\s+#\s+v\d+\.\d+\.\d+)?$/u);
  }
});

test("aggregates exactly four native targets and the minimum Node runtime", async () => {
  const fixture = await evidenceFixture();
  try {
    const aggregate = await verify(fixture);

    assert.deepEqual(aggregate.targets, Object.keys(targetRecords).sort());
    assert.equal(aggregate.commit_sha, commit);
    assert.equal(aggregate.version, version);
    assert.equal(aggregate.minimum_node_version, "v22.14.0");
    assert.equal(aggregate.platforms.length, 4);
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
        document.checks.security_gate = false;
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
      const path = fixture.paths.get("darwin-x64");
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
