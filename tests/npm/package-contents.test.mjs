import assert from "node:assert/strict";
import {
  chmod,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";

import {
  META_ALLOWLIST,
  PLATFORM_ALLOWLIST,
  validatePackageTree,
} from "../../scripts/npm/assemble.mjs";
import {
  npmPackFileList,
  validateStagedPackage,
} from "../../scripts/npm/pack.mjs";

const optionalDependencies = Object.fromEntries(
  [
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
  ].map((target) => [`@aiz.im/aizim-${target}`, "0.1.0"]),
);

async function createFiles(root, paths) {
  for (const path of paths) {
    if (path === "package.json") {
      continue;
    }
    const destination = join(root, path);
    await mkdir(dirname(destination), { recursive: true });
    await writeFile(destination, `${path}\n`);
  }
}

async function packageFixture(kind) {
  const root = await mkdtemp(join(tmpdir(), `aizim-${kind}-package-`));
  const isMeta = kind === "meta";
  const allowlist = isMeta ? META_ALLOWLIST : PLATFORM_ALLOWLIST;
  await createFiles(root, allowlist);
  const manifest = isMeta
    ? {
        name: "@aiz.im/aizim",
        version: "0.1.0",
        license: "MIT",
        type: "module",
        bin: { aizim: "bin/aizim.js" },
        files: [
          "bin/",
          "lib/",
          "manifest/",
          "vendor/",
          "LICENSE",
          "README.md",
          "THIRD_PARTY_NOTICES.md",
        ],
        publishConfig: { access: "public" },
        dependencies: { "@openai/codex": "0.145.0" },
        optionalDependencies,
      }
    : {
        name: "@aiz.im/aizim-darwin-arm64",
        version: "0.1.0",
        license: "MIT",
        main: "index.cjs",
        files: [
          "index.cjs",
          "bin/",
          "manifest/",
          "vendor/",
          "LICENSE",
          "README.md",
          "THIRD_PARTY_NOTICES.md",
        ],
        os: ["darwin"],
        cpu: ["arm64"],
        publishConfig: { access: "public" },
      };
  await writeFile(join(root, "package.json"), `${JSON.stringify(manifest)}\n`);
  for (const path of isMeta
    ? ["bin/aizim.js"]
    : ["bin/aizim-launcher", "vendor/uv"]) {
    await chmod(join(root, path), 0o755);
  }
  return { allowlist, root };
}

for (const kind of ["meta", "platform"]) {
  test(`npm packs the exact ${kind} package allowlist`, async () => {
    // Given
    const fixture = await packageFixture(kind);

    try {
      // When
      const packed = await npmPackFileList(fixture.root);

      // Then
      assert.deepEqual(packed, [...fixture.allowlist].sort());
      await validatePackageTree(fixture.root, fixture.allowlist);
      await validateStagedPackage(fixture.root, kind, "darwin-arm64");
    } finally {
      await rm(fixture.root, { force: true, recursive: true });
    }
  });
}

test("rejects forbidden names even when npm would omit them", async () => {
  const fixture = await packageFixture("meta");
  await writeFile(join(fixture.root, ".env"), "NPM_TOKEN=secret\n");

  try {
    await assert.rejects(
      validatePackageTree(fixture.root, fixture.allowlist),
      /package tree allowlist mismatch/,
    );
  } finally {
    await rm(fixture.root, { force: true, recursive: true });
  }
});

test("rejects missing executable modes and lifecycle scripts", async () => {
  const fixture = await packageFixture("platform");
  await chmod(join(fixture.root, "vendor", "uv"), 0o644);
  const manifestPath = join(fixture.root, "package.json");
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  manifest.scripts = { postinstall: "node install.js" };
  await writeFile(manifestPath, `${JSON.stringify(manifest)}\n`);

  try {
    await assert.rejects(
      validateStagedPackage(fixture.root, "platform", "darwin-arm64"),
      /package manifest or mode mismatch/,
    );
  } finally {
    await rm(fixture.root, { force: true, recursive: true });
  }
});
