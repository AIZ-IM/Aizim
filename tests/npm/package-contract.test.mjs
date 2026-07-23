import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const root = new URL("../../", import.meta.url);

function readJson(path) {
  return JSON.parse(readFileSync(new URL(path, root), "utf8"));
}

function readText(path) {
  return readFileSync(new URL(path, root), "utf8");
}

const platforms = [
  ["darwin-arm64", ["darwin"], ["arm64"], undefined],
  ["darwin-x64", ["darwin"], ["x64"], undefined],
  ["linux-arm64", ["linux"], ["arm64"], ["glibc"]],
  ["linux-x64", ["linux"], ["x64"], ["glibc"]],
];

test("uses one version and exact dependencies when manifests are loaded", () => {
  // Given
  const meta = readJson("package.json");
  const pyproject = readText("pyproject.toml");
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
  assert.equal(meta.engines.node, ">=22.14.0");
  assert.equal(meta.dependencies["@openai/codex"], "0.145.0");
  assert.equal(meta.devDependencies.typescript, "6.0.2");
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
