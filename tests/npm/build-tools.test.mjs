import assert from "node:assert/strict";
import {
  chmod,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { detectTarget } from "../../lib/platform.mjs";
import {
  artifactRecord,
  assertDigest,
  hashInputPaths,
  sha256File,
} from "../../scripts/npm/lib/hash.mjs";
import {
  detectBuildTarget,
  verifySourceVersions,
} from "../../scripts/npm/build.mjs";
import { fetchUv } from "../../scripts/npm/fetch-uv.mjs";
import { sourceInputPaths } from "../../scripts/npm/lib/paths.mjs";

async function temporaryDirectory() {
  return await mkdtemp(join(tmpdir(), "aizim-build-tools-"));
}

test("hashes files as lowercase SHA-256 and uses a timing-safe comparison", async () => {
  // Given
  const root = await temporaryDirectory();
  const path = join(root, "artifact");
  await writeFile(path, "aizim\n");

  try {
    // When
    const digest = await sha256File(path);
    let compared;
    assertDigest(digest, digest, (actual, expected) => {
      compared = [actual, expected];
      return true;
    });

    // Then
    assert.match(digest, /^[0-9a-f]{64}$/);
    assert.deepEqual(
      compared?.map((value) => value.toString("hex")),
      [digest, digest],
    );
    assert.throws(() => assertDigest(digest, digest.toUpperCase()));
  } finally {
    await rm(root, { force: true, recursive: true });
  }
});

test("deletes a mismatched uv archive without extracting it", async () => {
  // Given
  const root = await temporaryDirectory();
  const archivePath = join(root, "uv.tar.gz");
  const binaryPath = join(root, "uv");
  const extractionPath = join(root, "extract");
  const commands = [];
  const requests = [];

  try {
    // When
    await assert.rejects(
      fetchUv({
        target: "darwin-arm64",
        artifact: {
          archive: "uv-test.tar.gz",
          directory: "uv-test",
          sha256: "0".repeat(64),
        },
        archivePath,
        binaryPath,
        extractionPath,
        fetchImpl: async (...args) => {
          requests.push(args);
          return new Response("wrong archive");
        },
        runImpl: async (...args) => commands.push(args),
      }),
      /uv archive digest mismatch/,
    );

    // Then
    await assert.rejects(stat(archivePath), { code: "ENOENT" });
    assert.deepEqual(commands, []);
    assert.deepEqual(requests, [
      [
        "https://github.com/astral-sh/uv/releases/download/0.11.31/uv-test.tar.gz",
        { redirect: "follow" },
      ],
    ]);
  } finally {
    await rm(root, { force: true, recursive: true });
  }
});

test("extracts verified uv without a shell and requires an executable file", async () => {
  // Given
  const root = await temporaryDirectory();
  const archivePath = join(root, "uv.tar.gz");
  const binaryPath = join(root, "uv");
  const extractionPath = join(root, "extract");
  await writeFile(archivePath, "verified archive");
  const digest = await sha256File(archivePath);
  const commands = [];

  try {
    // When
    const result = await fetchUv({
      target: "darwin-arm64",
      artifact: {
        archive: "uv-test.tar.gz",
        directory: "uv-test",
        sha256: digest,
      },
      archivePath,
      binaryPath,
      extractionPath,
      fetchImpl: async () => {
        throw new Error("cached archives must be reused");
      },
      runImpl: async (program, args, options) => {
        commands.push([program, args, options]);
        const extracted = join(extractionPath, "uv-test", "uv");
        await mkdir(join(extractionPath, "uv-test"), { recursive: true });
        await writeFile(extracted, "#!/bin/sh\n");
        await chmod(extracted, 0o755);
      },
    });

    // Then
    assert.deepEqual(commands, [
      ["tar", ["-xzf", archivePath, "-C", extractionPath], undefined],
    ]);
    assert.equal((await stat(binaryPath)).isFile(), true);
    assert.equal((await stat(binaryPath)).mode & 0o111, 0o111);
    assert.equal(result.sha256, await sha256File(binaryPath));
  } finally {
    await rm(root, { force: true, recursive: true });
  }
});

test("rejects a non-executable extracted uv binary", async () => {
  // Given
  const root = await temporaryDirectory();
  const archivePath = join(root, "uv.tar.gz");
  const binaryPath = join(root, "uv");
  const extractionPath = join(root, "extract");
  await writeFile(archivePath, "verified archive");

  try {
    // When / Then
    await assert.rejects(
      fetchUv({
        target: "darwin-arm64",
        artifact: {
          archive: "uv-test.tar.gz",
          directory: "uv-test",
          sha256: await sha256File(archivePath),
        },
        archivePath,
        binaryPath,
        extractionPath,
        fetchImpl: async () => new Response(),
        runImpl: async () => {
          const extracted = join(extractionPath, "uv-test", "uv");
          await mkdir(join(extractionPath, "uv-test"), { recursive: true });
          await writeFile(extracted, "not executable\n");
          await chmod(extracted, 0o644);
        },
      }),
      /uv executable is invalid/,
    );
  } finally {
    await rm(root, { force: true, recursive: true });
  }
});

test("build target detection agrees with runtime target detection", () => {
  for (const [platform, arch, report] of [
    ["darwin", "arm64", undefined],
    ["darwin", "x64", undefined],
    ["linux", "arm64", { header: { glibcVersionRuntime: "2.39" } }],
    ["linux", "x64", { header: { glibcVersionRuntime: "2.39" } }],
  ]) {
    assert.equal(
      detectBuildTarget({ platform, arch, report }).id,
      detectTarget({ platform, arch, report }).id,
    );
  }
});

test("rejects a version mismatch across source manifests", () => {
  const versions = {
    meta: "0.1.0",
    python: "0.1.0",
    cargo: "0.1.0",
    platforms: ["0.1.0", "0.1.0", "0.2.0", "0.1.0"],
  };

  assert.throws(() => verifySourceVersions(versions), /version mismatch/);
  assert.doesNotThrow(() =>
    verifySourceVersions({
      ...versions,
      platforms: versions.platforms.map(() => "0.1.0"),
    }),
  );
});

test("measures artifact size and digest from file content", async () => {
  const root = await temporaryDirectory();
  const path = join(root, "artifact");
  await writeFile(path, "measured bytes\n");

  try {
    assert.deepEqual(await artifactRecord(path, "vendor/artifact"), {
      path: "vendor/artifact",
      sha256: await sha256File(path),
      size: 15,
    });
  } finally {
    await rm(root, { force: true, recursive: true });
  }
});

test("source digest covers every declared input and ignores generated output", async () => {
  const root = await temporaryDirectory();
  const first = join(root, "first");
  const second = join(root, "second");
  const generated = join(root, "build", "npm", "generated");
  await writeFile(first, "one");
  await writeFile(second, "two");
  await mkdir(join(root, "build", "npm"), { recursive: true });
  await writeFile(generated, "initial");

  try {
    const initial = await hashInputPaths(root, ["first", "second"]);
    await writeFile(generated, "changed");
    assert.equal(await hashInputPaths(root, ["first", "second"]), initial);

    for (const [path, value] of [
      [first, "changed one"],
      [second, "changed two"],
    ]) {
      await writeFile(path, value);
      assert.notEqual(await hashInputPaths(root, ["first", "second"]), initial);
      await writeFile(path, path === first ? "one" : "two");
    }
    assert.equal(await readFile(first, "utf8"), "one");
  } finally {
    await rm(root, { force: true, recursive: true });
  }
});

test("source input discovery ignores Python bytecode caches", async () => {
  const root = await temporaryDirectory();
  const source = join(root, "src", "aizim", "worker.py");
  const bytecode = join(
    root,
    "src",
    "aizim",
    "__pycache__",
    "worker.cpython-312.pyc",
  );
  await mkdir(join(root, "src", "aizim", "__pycache__"), {
    recursive: true,
  });
  await writeFile(source, "def run(): ...\n");
  await writeFile(bytecode, "generated");

  try {
    assert.deepEqual(await sourceInputPaths(root, ["src"]), [
      "src/aizim/worker.py",
    ]);
  } finally {
    await rm(root, { force: true, recursive: true });
  }
});
