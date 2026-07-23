import {
  lstat,
  mkdir,
  readFile,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { basename, dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  META_ALLOWLIST,
  PLATFORM_ALLOWLIST,
  validatePackageTree,
} from "./assemble.mjs";
import { capture } from "./lib/command.mjs";
import {
  assertDigest,
  hashInputPaths,
  sha256File,
} from "./lib/hash.mjs";
import {
  buildSummaryPath,
  metaBuildPath,
  npmDistRoot,
  repositoryRoot,
  sourceInputPaths,
  targetBuildPath,
} from "./lib/paths.mjs";

const targets = Object.freeze({
  "darwin-arm64": { os: ["darwin"], cpu: ["arm64"] },
  "darwin-x64": { os: ["darwin"], cpu: ["x64"] },
  "linux-arm64": { os: ["linux"], cpu: ["arm64"], libc: ["glibc"] },
  "linux-x64": { os: ["linux"], cpu: ["x64"], libc: ["glibc"] },
});

const forbiddenLifecycle = new Set([
  "install",
  "postinstall",
  "preinstall",
  "prepare",
  "prepublish",
]);

async function readJson(path) {
  return JSON.parse(await readFile(path, "utf8"));
}

function oneNpmResult(raw) {
  const values = Object.values(JSON.parse(raw));
  if (values.length !== 1) {
    throw new Error("npm pack output mismatch");
  }
  return values[0];
}

async function checkExecutable(path) {
  const metadata = await lstat(path);
  return (
    metadata.isFile() &&
    !metadata.isSymbolicLink() &&
    metadata.nlink === 1 &&
    (metadata.mode & 0o777) === 0o755
  );
}

function exactObject(actual, expected) {
  return JSON.stringify(actual) === JSON.stringify(expected);
}

export async function validateStagedPackage(root, kind, target) {
  try {
    const manifest = await readJson(join(root, "package.json"));
    const lifecycle = Object.keys(manifest.scripts ?? {}).filter((name) =>
      forbiddenLifecycle.has(name),
    );
    if (
      manifest.version !== "0.1.0" ||
      manifest.private !== undefined ||
      manifest.publishConfig?.access !== "public" ||
      lifecycle.length > 0
    ) {
      throw new Error("manifest");
    }
    if (kind === "meta") {
      const optionalDependencies = Object.fromEntries(
        Object.keys(targets).map((id) => [`@aiz.im/aizim-${id}`, "0.1.0"]),
      );
      if (
        manifest.name !== "@aiz.im/aizim" ||
        manifest.dependencies?.["@openai/codex"] !== "0.145.0" ||
        !exactObject(manifest.optionalDependencies, optionalDependencies) ||
        manifest.os !== undefined ||
        manifest.cpu !== undefined ||
        manifest.libc !== undefined ||
        !(await checkExecutable(join(root, "bin/aizim.js")))
      ) {
        throw new Error("meta");
      }
      return;
    }
    const expected = targets[target];
    if (
      !expected ||
      manifest.name !== `@aiz.im/aizim-${target}` ||
      !exactObject(manifest.os, expected.os) ||
      !exactObject(manifest.cpu, expected.cpu) ||
      !exactObject(manifest.libc, expected.libc) ||
      !(await checkExecutable(join(root, "bin/aizim-launcher"))) ||
      !(await checkExecutable(join(root, "vendor/uv")))
    ) {
      throw new Error("platform");
    }
  } catch {
    throw new Error("package manifest or mode mismatch");
  }
}

export async function npmPackFileList(root) {
  const raw = await capture("npm", ["pack", "--dry-run", "--json"], {
    cwd: root,
  });
  return oneNpmResult(raw).files.map(({ path }) => path).sort();
}

async function verifyArtifact(root, artifact) {
  const path = join(root, artifact.path);
  const metadata = await lstat(path);
  if (!metadata.isFile() || metadata.size !== artifact.size) {
    throw new Error("manifest artifact mismatch");
  }
  assertDigest(await sha256File(path), artifact.sha256);
}

export async function verifyFreshBuild() {
  const summary = await readJson(buildSummaryPath);
  if (
    summary.schema_version !== 1 ||
    !targets[summary.target] ||
    summary.git_sha !==
      (await capture("git", ["rev-parse", "HEAD"], { cwd: repositoryRoot }))
  ) {
    throw new Error("build summary mismatch");
  }
  const inputs = await sourceInputPaths();
  const digest = await hashInputPaths(repositoryRoot, inputs);
  assertDigest(digest, summary.source_input_sha256);
  for (const artifact of Object.values(summary.artifacts)) {
    const path = join(repositoryRoot, artifact.path);
    assertDigest(await sha256File(path), artifact.sha256);
  }

  const metaRoot = metaBuildPath;
  const platformRoot = targetBuildPath(summary.target);
  const distribution = await readJson(
    join(metaRoot, "manifest/distribution.json"),
  );
  const platform = await readJson(
    join(platformRoot, "manifest/platform.json"),
  );
  await verifyArtifact(metaRoot, distribution.wheel);
  await verifyArtifact(metaRoot, distribution.runtime_requirements);
  await verifyArtifact(platformRoot, platform.uv);
  await validatePackageTree(metaRoot, META_ALLOWLIST);
  await validatePackageTree(platformRoot, PLATFORM_ALLOWLIST);
  await validateStagedPackage(metaRoot, "meta", summary.target);
  await validateStagedPackage(platformRoot, "platform", summary.target);
  return summary;
}

function assertPackedFiles(result, allowlist) {
  const actual = result.files.map(({ path }) => path).sort();
  const expected = [...allowlist].sort();
  if (
    actual.length !== expected.length ||
    actual.some((path, index) => path !== expected[index])
  ) {
    throw new Error("packed file allowlist mismatch");
  }
}

async function packOne(root, allowlist) {
  const raw = await capture(
    "npm",
    ["pack", "--json", "--pack-destination", npmDistRoot, root],
    { cwd: repositoryRoot },
  );
  const result = oneNpmResult(raw);
  assertPackedFiles(result, allowlist);
  const path = join(npmDistRoot, basename(result.filename));
  return {
    name: result.name,
    version: result.version,
    filename: basename(result.filename),
    size: (await stat(path)).size,
    sha256: await sha256File(path),
    integrity: result.integrity,
  };
}

export async function pack() {
  const summary = await verifyFreshBuild();
  await rm(npmDistRoot, { force: true, recursive: true });
  await mkdir(npmDistRoot, { mode: 0o700, recursive: true });
  const platformRoot = targetBuildPath(summary.target);
  const packages = [
    await packOne(platformRoot, PLATFORM_ALLOWLIST),
    await packOne(metaBuildPath, META_ALLOWLIST),
  ];
  const output = {
    schema_version: 1,
    git_sha: summary.git_sha,
    target: summary.target,
    packages,
  };
  const path = join(npmDistRoot, "pack-summary.json");
  await mkdir(dirname(path), { mode: 0o700, recursive: true });
  await writeFile(path, `${JSON.stringify(output, null, 2)}\n`, {
    mode: 0o644,
  });
  process.stdout.write(
    `${packages.map(({ filename }) => relative(repositoryRoot, join(npmDistRoot, filename))).join("\n")}\n`,
  );
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : undefined;
if (invokedPath === fileURLToPath(import.meta.url)) {
  await pack();
}
