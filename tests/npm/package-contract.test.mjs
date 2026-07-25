import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

import { detectTarget } from "../../lib/platform.mjs";

const root = new URL("../../", import.meta.url);

function readJson(path) {
  return JSON.parse(readFileSync(new URL(path, root), "utf8"));
}

function readText(path) {
  return readFileSync(new URL(path, root), "utf8");
}

const platforms = [
  ["darwin-arm64", ["darwin"], ["arm64"], undefined],
  ["linux-arm64", ["linux"], ["arm64"], ["glibc"]],
  ["linux-x64", ["linux"], ["x64"], ["glibc"]],
];

test("uses one version without owning provider packages", () => {
  // Given
  const meta = readJson("package.json");
  const pyproject = readText("pyproject.toml");
  const pythonVersion = readText(".python-version").trim();
  const cargo = readText("Cargo.toml");

  // When
  const sourcePackages = platforms.map(([target]) => [
    target,
    readJson(`npm/platforms/${target}/package.json`),
  ]);

  // Then
  assert.equal(meta.name, "@aiz.im/aizim");
  assert.equal(meta.version, "0.1.0");
  assert.equal(meta.packageManager, "npm@12.0.1");
  assert.equal(meta.engines.node, ">=22.22.2");
  assert.equal(meta.dependencies, undefined);
  assert.equal(meta.peerDependencies, undefined);
  assert.equal(meta.devDependencies.typescript, "6.0.2");
  assert.equal(pythonVersion, "3.14.6");
  assert.deepEqual(meta.workspaces, ["npm/platforms/*"]);
  assert.ok(meta.files.includes("lib/"));
  assert.match(pyproject, /^version = "0\.1\.0"$/m);
  assert.match(cargo, /^version = "0\.1\.0"$/m);

  for (const [target, pkg] of sourcePackages) {
    const name = `@aiz.im/aizim-${target}`;
    assert.equal(pkg.name, name);
    assert.equal(pkg.version, meta.version);
    assert.equal(meta.optionalDependencies[name], meta.version);
    assert.equal(pkg.private, true);
    assert.equal("os" in pkg, false);
    assert.equal("cpu" in pkg, false);
    assert.equal("libc" in pkg, false);
  }
});

test("retains platform selectors when public package manifests are inspected", () => {
  // Given
  const meta = readJson("package.json");

  for (const [target, os, cpu, libc] of platforms) {
    const publishPath = `npm/platforms/${target}/package.publish.json`;

    // When
    const publishManifestExists = existsSync(new URL(publishPath, root));

    // Then
    assert.equal(publishManifestExists, true, publishPath);
    const pkg = readJson(publishPath);
    assert.equal(pkg.name, `@aiz.im/aizim-${target}`);
    assert.equal(pkg.version, meta.version);
    assert.equal(pkg.private, undefined);
    assert.deepEqual(pkg.os, os);
    assert.deepEqual(pkg.cpu, cpu);
    assert.deepEqual(pkg.libc, libc);
    assert.equal(pkg.publishConfig.access, "public");
  }
});

test("maps exactly three Aizim-owned native targets", () => {
  const targets = [
    ["darwin", "arm64", "darwin-arm64", "aarch64-apple-darwin"],
    ["linux", "arm64", "linux-arm64", "aarch64-unknown-linux-gnu"],
    ["linux", "x64", "linux-x64", "x86_64-unknown-linux-gnu"],
  ];

  for (const [platform, arch, id, rustTarget] of targets) {
    const target = detectTarget({
      platform,
      arch,
      report:
        platform === "linux"
          ? { header: { glibcVersionRuntime: "2.39" } }
          : undefined,
    });
    assert.deepEqual(target, {
      id,
      packageName: `@aiz.im/aizim-${id}`,
      rustTarget,
    });
  }
  assert.throws(
    () => detectTarget({ platform: "darwin", arch: "x64" }),
    /unsupported platform/u,
  );
});

test("declares no package lifecycle hook when installation metadata is inspected", () => {
  // Given
  const forbidden = new Set([
    "preinstall",
    "install",
    "postinstall",
    "prepare",
    "prepublish",
  ]);
  const manifests = [
    "package.json",
    ...platforms.map(([target]) => `npm/platforms/${target}/package.json`),
    ...platforms.map(([target]) => `npm/platforms/${target}/package.publish.json`),
  ];

  // When
  const lifecycleHooks = manifests.flatMap((path) => {
    const scripts = readJson(path).scripts ?? {};
    return Object.keys(scripts)
      .filter((name) => forbidden.has(name))
      .map((name) => `${path}:${name}`);
  });

  // Then
  assert.deepEqual(lifecycleHooks, []);
});

test("commits a dependency-free Lake manifest for the smoke project", () => {
  const manifest = readJson("examples/smoke_lean/lake-manifest.json");

  assert.equal(manifest.name, "aizimSmoke");
  assert.deepEqual(manifest.packages, []);
});

test("documents external agent ownership, compatibility, and upgrade recovery", () => {
  const readme = readText("README.md");
  const npmReadme = readText("npm/README.md");
  const distribution = readText("docs/operations/npm-distribution.md");
  const foundation = readText("docs/operations/foundation-runbook.md");

  for (const document of [readme, npmReadme, distribution]) {
    assert.match(document, /does not install Codex or Claude/u);
    assert.match(document, /Codex CLI 0\.145\.0/u);
    assert.match(document, /Claude Code 2\.1\.218/u);
  }
  const operations = `${distribution}\n${foundation}`;
  assert.match(
    operations,
    /Codex is always required for Worker and sandbox readiness/u,
  );
  assert.match(
    operations,
    /Claude is required only when `claude`\s+is the selected Controller/u,
  );
  assert.match(
    operations,
    /`AIZIM_CODEX_EXECUTABLE` before `codex` on `PATH`/u,
  );
  assert.match(
    operations,
    /`AIZIM_CLAUDE_EXECUTABLE` before `claude` on `PATH`/u,
  );
  assert.match(operations, /observed=.*supported=/u);
  assert.match(operations, /supported side-by-side CLI/u);
  assert.match(operations, /trust on first use/u);
  assert.match(operations, /canonical path, exact version, and\s+SHA-256/u);
  assert.match(operations, /user-owned provider provenance/u);
  assert.match(operations, /preserves project state and credentials/u);
  assert.match(operations, /does not use the old nested agent packages/u);

  for (const document of [readme, npmReadme, distribution, foundation]) {
    assert.doesNotMatch(
      document,
      /package-local (?:Codex|Claude)|supplies Codex|bundled Codex/u,
    );
  }
});
