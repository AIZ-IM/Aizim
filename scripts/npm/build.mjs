import {
  chmod,
  copyFile,
  mkdir,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises";
import { basename, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { detectTarget } from "../../lib/platform.mjs";
import { assemblePackages } from "./assemble.mjs";
import { fetchUv } from "./fetch-uv.mjs";
import { capture, run } from "./lib/command.mjs";
import {
  artifactRecord,
  hashInputPaths,
  sha256File,
} from "./lib/hash.mjs";
import {
  buildSummaryPath,
  metaBuildPath,
  npmBuildRoot,
  repositoryRoot,
  requirementsBuildPath,
  sourceInputPaths,
  targetBuildPath,
  uvArchivePath,
  uvExtractionPath,
  uvToolPath,
  wheelBuildPath,
} from "./lib/paths.mjs";
import { writeNotices } from "./write-notices.mjs";

const expected = Object.freeze({
  aizim: "0.1.0",
  codex: "0.145.0",
  node: "26.5.0",
  npm: "12.0.1",
  python: "3.12",
  rust: "1.97.1",
  uv: "0.11.31",
});

const platformIds = [
  "darwin-arm64",
  "darwin-x64",
  "linux-arm64",
  "linux-x64",
];

async function readJson(path) {
  return JSON.parse(await readFile(path, "utf8"));
}

function tomlVersion(text) {
  const match = text.match(/^version = "([^"]+)"$/m);
  if (!match) {
    throw new Error("source version is missing");
  }
  return match[1];
}

async function writeJson(path, value) {
  await writeFile(path, `${JSON.stringify(value, null, 2)}\n`, {
    mode: 0o644,
  });
}

export function detectBuildTarget(options) {
  return detectTarget(options);
}

export function verifySourceVersions({
  meta,
  python,
  cargo,
  platforms,
}) {
  const versions = [meta, python, cargo, ...platforms];
  if (versions.some((version) => version !== expected.aizim)) {
    throw new Error("Aizim source version mismatch");
  }
}

async function sourceVersions() {
  const meta = await readJson(join(repositoryRoot, "package.json"));
  const python = await readFile(join(repositoryRoot, "pyproject.toml"), "utf8");
  const cargo = await readFile(join(repositoryRoot, "Cargo.toml"), "utf8");
  const platforms = await Promise.all(
    platformIds.map(async (target) => {
      const source = await readJson(
        join(repositoryRoot, "npm", "platforms", target, "package.json"),
      );
      const published = await readJson(
        join(
          repositoryRoot,
          "npm",
          "platforms",
          target,
          "package.publish.json",
        ),
      );
      if (source.version !== published.version) {
        throw new Error("platform source version mismatch");
      }
      return published.version;
    }),
  );
  verifySourceVersions({
    meta: meta.version,
    python: tomlVersion(python),
    cargo: tomlVersion(cargo),
    platforms,
  });
  if (
    meta.packageManager !== `npm@${expected.npm}` ||
    meta.dependencies?.["@openai/codex"] !== expected.codex ||
    !python.includes(`requires-python = ">=${expected.python},`) ||
    !python.includes(`uv_build==${expected.uv}`) ||
    !cargo.includes(`rust-version = "${expected.rust}"`)
  ) {
    throw new Error("toolchain source version mismatch");
  }
  return meta;
}

async function verifyToolchain(target) {
  if (process.versions.node !== expected.node) {
    throw new Error("Node.js version mismatch");
  }
  if ((await capture("npm", ["--version"], { cwd: repositoryRoot })) !== expected.npm) {
    throw new Error("npm version mismatch");
  }
  const rust = await capture("rustc", ["--version", "--verbose"], {
    cwd: repositoryRoot,
  });
  const release = rust.match(/^release: (.+)$/m)?.[1];
  const host = rust.match(/^host: (.+)$/m)?.[1];
  if (release !== expected.rust || host !== target.rustTarget) {
    throw new Error("Rust toolchain or host mismatch");
  }
}

async function resetBuildOutputs(target) {
  for (const path of [
    metaBuildPath,
    targetBuildPath(target),
    wheelBuildPath,
    requirementsBuildPath,
    buildSummaryPath,
  ]) {
    await rm(path, { force: true, recursive: true });
  }
  await mkdir(wheelBuildPath, { mode: 0o700, recursive: true });
}

async function builtWheel() {
  const files = (await readdir(wheelBuildPath)).filter((name) =>
    name.endsWith(".whl"),
  );
  if (files.length !== 1 || files[0] !== "aizim-0.1.0-py3-none-any.whl") {
    throw new Error("wheel output mismatch");
  }
  return join(wheelBuildPath, files[0]);
}

async function assembleArtifacts(target, uv) {
  const metaVendor = join(metaBuildPath, "vendor");
  const metaManifest = join(metaBuildPath, "manifest");
  const platformRoot = targetBuildPath(target.id);
  const platformBin = join(platformRoot, "bin");
  const platformVendor = join(platformRoot, "vendor");
  const platformManifest = join(platformRoot, "manifest");
  await Promise.all([
    mkdir(metaVendor, { mode: 0o700, recursive: true }),
    mkdir(metaManifest, { mode: 0o700, recursive: true }),
    mkdir(platformBin, { mode: 0o700, recursive: true }),
    mkdir(platformVendor, { mode: 0o700, recursive: true }),
    mkdir(platformManifest, { mode: 0o700, recursive: true }),
  ]);

  const wheelSource = await builtWheel();
  const wheelName = basename(wheelSource);
  const wheel = join(metaVendor, wheelName);
  const requirements = join(metaVendor, "runtime-requirements.txt");
  const launcher = join(platformBin, "aizim-launcher");
  const packagedUv = join(platformVendor, "uv");
  await Promise.all([
    copyFile(wheelSource, wheel),
    copyFile(requirementsBuildPath, requirements),
    copyFile(join(repositoryRoot, "target", "release", "aizim-launcher"), launcher),
    copyFile(uv.path, packagedUv),
  ]);
  await Promise.all([
    chmod(wheel, 0o644),
    chmod(requirements, 0o644),
    chmod(launcher, 0o755),
    chmod(packagedUv, 0o755),
  ]);

  const distribution = {
    schema_version: 1,
    aizim_version: expected.aizim,
    python_version: expected.python,
    wheel: await artifactRecord(wheel, `vendor/${wheelName}`),
    runtime_requirements: await artifactRecord(
      requirements,
      "vendor/runtime-requirements.txt",
    ),
    codex_version: expected.codex,
    minimum_node_version: "22.14.0",
    platform_schema_version: 1,
  };
  const platform = {
    schema_version: 1,
    aizim_version: expected.aizim,
    package_name: target.packageName,
    target: target.id,
    node_platform: process.platform,
    node_arch: process.arch,
    rust_target: target.rustTarget,
    libc: target.id.startsWith("linux-") ? "glibc" : null,
    launcher_version: expected.aizim,
    uv_version: expected.uv,
    uv: await artifactRecord(packagedUv, "vendor/uv"),
    distribution_schema_version: 1,
  };
  const distributionPath = join(metaManifest, "distribution.json");
  const platformPath = join(platformManifest, "platform.json");
  await writeJson(distributionPath, distribution);
  await writeJson(platformPath, platform);
  return {
    distributionPath,
    launcher,
    platformPath,
    requirements,
    uv: packagedUv,
    wheel,
  };
}

async function trackedInputsAreDirty(inputs) {
  const output = await capture(
    "git",
    ["status", "--porcelain=v1", "--untracked-files=no", "--", ...inputs],
    { cwd: repositoryRoot },
  );
  return output !== "";
}

async function writeBuildSummary(target, artifacts, inputs, sourceDigest) {
  const values = {};
  for (const [name, path] of Object.entries(artifacts)) {
    values[name] = {
      path: relative(repositoryRoot, path),
      sha256: await sha256File(path),
    };
  }
  await mkdir(npmBuildRoot, { mode: 0o700, recursive: true });
  await writeJson(buildSummaryPath, {
    schema_version: 1,
    git_sha: await capture("git", ["rev-parse", "HEAD"], {
      cwd: repositoryRoot,
    }),
    dirty_tracked_inputs: await trackedInputsAreDirty(inputs),
    target: target.id,
    source_input_sha256: sourceDigest,
    artifacts: values,
  });
}

export async function build() {
  const target = detectBuildTarget();
  await writeNotices();
  const meta = await sourceVersions();
  await verifyToolchain(target);
  await resetBuildOutputs(target.id);

  const uvArtifactTable = await readJson(
    join(repositoryRoot, "scripts", "npm", "uv-artifacts.json"),
  );
  if (uvArtifactTable.version !== expected.uv) {
    throw new Error("uv version mismatch");
  }
  const artifact = uvArtifactTable.targets[target.id];
  const uv = await fetchUv({
    target: target.id,
    artifact,
    version: uvArtifactTable.version,
    archivePath: uvArchivePath(target.id, artifact.archive),
    extractionPath: uvExtractionPath(target.id),
    binaryPath: uvToolPath(target.id),
  });

  await run(
    uv.path,
    ["--no-config", "build", "--wheel", "--out-dir", wheelBuildPath],
    { cwd: repositoryRoot },
  );
  await run(
    uv.path,
    [
      "--no-config",
      "export",
      "--locked",
      "--no-dev",
      "--no-emit-project",
      "--format",
      "requirements.txt",
      "--output-file",
      requirementsBuildPath,
    ],
    { cwd: repositoryRoot },
  );
  await run(
    "cargo",
    ["build", "--release", "--locked", "-p", "aizim-launcher"],
    { cwd: repositoryRoot },
  );
  const artifacts = await assembleArtifacts(target, uv);
  await assemblePackages(target.id, artifacts);
  const inputs = await sourceInputPaths();
  const sourceDigest = await hashInputPaths(repositoryRoot, inputs);
  await writeBuildSummary(target, artifacts, inputs, sourceDigest);
  process.stdout.write(
    `Built ${meta.name} ${meta.version} for ${target.id}\n`,
  );
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : undefined;
if (invokedPath === fileURLToPath(import.meta.url)) {
  await build();
}
