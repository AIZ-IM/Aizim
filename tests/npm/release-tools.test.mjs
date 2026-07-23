import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  RegistryMismatchError,
  RegistryNotFoundError,
  verifyRegistryPackage,
} from "../../scripts/npm/registry-verify.mjs";
import {
  createRegistrySmokeAggregate,
} from "../../scripts/npm/registry-smoke.mjs";
import {
  verifyReleaseBundle,
  readSourceVersions,
  validateVersionSet,
} from "../../scripts/npm/verify-release-bundle.mjs";
import {
  renderReleaseRecord,
} from "../../scripts/npm/write-release-record.mjs";
import {
  CI_TARGETS,
  INSTALL_CHECK_NAMES,
} from "../../scripts/npm/verify-ci-evidence.mjs";

const commit = "1".repeat(40);
const version = "0.1.0";
const integrity = (content) =>
  `sha512-${createHash("sha512").update(content).digest("base64")}`;
const sha256 = (content) =>
  createHash("sha256").update(content).digest("hex");

async function releaseFixture() {
  const root = await mkdtemp(join(tmpdir(), "aizim-release-test-"));
  const metaContent = Buffer.from("common-meta");
  const metaFilename = `aiz.im-aizim-${version}.tgz`;
  await writeFile(join(root, metaFilename), metaContent);

  for (const [target, runner] of Object.entries(CI_TARGETS)) {
    const platformContent = Buffer.from(`platform-${target}`);
    const platformFilename = `aiz.im-aizim-${target}-${version}.tgz`;
    await writeFile(join(root, platformFilename), platformContent);
    await writeFile(
      join(root, `native-evidence-${target}.json`),
      `${JSON.stringify({
        schema_version: 1,
        commit_sha: commit,
        version,
        target,
        dirty_tracked_inputs: false,
        restored_build_cache: false,
        runner,
        packages: [
          {
            name: `@aiz.im/aizim-${target}`,
            filename: platformFilename,
            size: platformContent.length,
            sha256: sha256(platformContent),
            integrity: integrity(platformContent),
          },
          {
            name: "@aiz.im/aizim",
            filename: metaFilename,
            size: metaContent.length,
            sha256: sha256(metaContent),
            integrity: integrity(metaContent),
          },
        ],
        checks: {
          source_build: true,
          npm_test: true,
          ...Object.fromEntries(
            INSTALL_CHECK_NAMES.map((name) => [name, true]),
          ),
        },
      })}\n`,
    );
  }

  return {
    root,
    output: join(root, "aggregate.json"),
    sourceVersions: {
      python: version,
      npm: version,
      cargo: version,
      platforms: Object.fromEntries(
        Object.keys(CI_TARGETS).map((target) => [target, version]),
      ),
    },
    cleanup: async () => rm(root, { force: true, recursive: true }),
  };
}

test("verifies a clean three-target release bundle with one common meta tarball", async () => {
  const fixture = await releaseFixture();
  try {
    const aggregate = await verifyReleaseBundle({
      directory: fixture.root,
      commit,
      version,
      output: fixture.output,
      sourceVersions: fixture.sourceVersions,
    });
    assert.deepEqual(aggregate.targets, Object.keys(CI_TARGETS).sort());
    assert.equal(aggregate.meta.integrity, integrity("common-meta"));
    assert.equal(aggregate.platforms.length, Object.keys(CI_TARGETS).length);
    assert.deepEqual(
      JSON.parse(await readFile(fixture.output, "utf8")),
      aggregate,
    );
  } finally {
    await fixture.cleanup();
  }
});

test("rejects dirty tracked inputs and cross-language version drift", async (context) => {
  await context.test("dirty tracked inputs", async () => {
    const fixture = await releaseFixture();
    try {
      const path = join(
        fixture.root,
        "native-evidence-darwin-arm64.json",
      );
      const document = JSON.parse(await readFile(path, "utf8"));
      document.dirty_tracked_inputs = true;
      await writeFile(path, `${JSON.stringify(document)}\n`);
      await assert.rejects(
        verifyReleaseBundle({
          directory: fixture.root,
          commit,
          version,
          output: fixture.output,
          sourceVersions: fixture.sourceVersions,
        }),
        /dirty tracked inputs/u,
      );
    } finally {
      await fixture.cleanup();
    }
  });

  await context.test("version drift", () => {
    assert.throws(
      () =>
        validateVersionSet(
          {
            python: version,
            npm: version,
            cargo: "0.2.0",
            platforms: Object.fromEntries(
              Object.keys(CI_TARGETS).map((target) => [target, version]),
            ),
          },
          version,
        ),
      /version mismatch/u,
    );
  });
});

test("reads the same release version from Python, npm, Cargo, and every platform", async () => {
  validateVersionSet(await readSourceVersions(), version);
});

test("registry verification distinguishes 404 and integrity mismatch", async () => {
  await assert.rejects(
    verifyRegistryPackage({
      name: "@aiz.im/aizim",
      version,
      integrity: "sha512-expected",
      runNpm: async () => ({
        code: 1,
        stdout: "",
        stderr: "npm error code E404",
      }),
    }),
    RegistryNotFoundError,
  );
  await assert.rejects(
    verifyRegistryPackage({
      name: "@aiz.im/aizim",
      version,
      integrity: "sha512-expected",
      runNpm: async () => ({
        code: 0,
        stdout: JSON.stringify({
          version,
          "dist.integrity": "sha512-other",
        }),
        stderr: "",
      }),
    }),
    RegistryMismatchError,
  );
});

test("registry wait mode stops after twelve 404 attempts at five-second intervals", async () => {
  let attempts = 0;
  const delays = [];
  await assert.rejects(
    verifyRegistryPackage({
      name: "@aiz.im/aizim",
      version,
      integrity: "sha512-expected",
      wait: true,
      runNpm: async () => {
        attempts += 1;
        return {
          code: 1,
          stdout: "",
          stderr: "npm error code E404",
        };
      },
      delay: async (milliseconds) => delays.push(milliseconds),
    }),
    RegistryNotFoundError,
  );
  assert.equal(attempts, 12);
  assert.deepEqual(delays, Array(11).fill(5_000));
});

test("registry smoke aggregate requires READY, Gate B, and a run on all targets", () => {
  const documents = Object.keys(CI_TARGETS).map((target) => ({
    schema_version: 1,
    version,
    target,
    checks: {
      ready: true,
      security_gate: true,
      aizim_run: true,
    },
  }));
  const aggregate = createRegistrySmokeAggregate(documents, version);
  assert.deepEqual(aggregate.targets, Object.keys(CI_TARGETS).sort());
  documents[0].checks.ready = false;
  assert.throws(
    () => createRegistrySmokeAggregate(documents, version),
    /registry smoke checks/u,
  );
});

test("release record is deterministic and excludes sensitive runtime context", () => {
  const input = {
    aggregate: {
      schema_version: 1,
      commit_sha: commit,
      version,
      targets: Object.keys(CI_TARGETS).sort(),
      meta: { integrity: "sha512-meta" },
      platforms: Object.keys(CI_TARGETS).map((target) => ({
        target,
        integrity: `sha512-${target}`,
      })),
    },
    receipts: [
      {
        name: "@aiz.im/aizim",
        version,
        integrity: "sha512-meta",
      },
      ...Object.keys(CI_TARGETS).map((target) => ({
        name: `@aiz.im/aizim-${target}`,
        version,
        integrity: `sha512-${target}`,
      })),
    ],
    smoke: {
      schema_version: 1,
      version,
      targets: Object.keys(CI_TARGETS).sort(),
    },
    runIds: {
      release: "101",
      registry_smoke: "102",
    },
  };
  const first = renderReleaseRecord(input);
  const second = renderReleaseRecord(input);
  assert.equal(first, second);
  assert.match(first, /private repository provenance/u);
  assert.match(first, /no credentialed model run/u);
  assert.doesNotMatch(
    first,
    /\/Users\/|\/tmp\/|NODE_AUTH_TOKEN|NPM_TOKEN|Bearer |process\.env/u,
  );
});
