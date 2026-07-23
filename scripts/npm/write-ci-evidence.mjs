import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import {
  mkdir,
  readFile,
  rename,
  stat,
  writeFile,
} from "node:fs/promises";
import {
  basename,
  dirname,
  isAbsolute,
  join,
  resolve,
} from "node:path";
import { fileURLToPath } from "node:url";

import { detectTarget } from "../../lib/platform.mjs";
import { capture } from "./lib/command.mjs";
import {
  npmBuildRoot,
  npmDistRoot,
  repositoryRoot,
} from "./lib/paths.mjs";
import { verifyFreshBuild } from "./pack.mjs";
import {
  CI_TARGETS,
  INSTALL_CHECK_NAMES,
} from "./verify-ci-evidence.mjs";

const installEvidencePath = join(
  npmBuildRoot,
  "install-smoke-evidence.json",
);
const testSummaryPath = join(npmBuildRoot, "test-summary.json");
const packSummaryPath = join(npmDistRoot, "pack-summary.json");
const fullSha = /^[0-9a-f]{40}$/u;
const nodeVersion = /^v\d+\.\d+\.\d+$/u;

function object(value, label) {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype
  ) {
    throw new Error(`${label} must be an object`);
  }
  return value;
}

function exactKeys(value, expected, label) {
  const actual = Object.keys(object(value, label)).sort();
  const wanted = [...expected].sort();
  if (
    actual.length !== wanted.length ||
    actual.some((key, index) => key !== wanted[index])
  ) {
    throw new Error(`${label} fields mismatch`);
  }
}

async function readJson(path, label) {
  try {
    return object(JSON.parse(await readFile(path, "utf8")), label);
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error(`${label} is invalid`);
    }
    throw error;
  }
}

function validateReadyRecord(value) {
  exactKeys(value, ["sha256", "mtime_ns"], "ready record");
  if (
    typeof value.sha256 !== "string" ||
    !/^[0-9a-f]{64}$/u.test(value.sha256) ||
    typeof value.mtime_ns !== "string" ||
    !/^\d+$/u.test(value.mtime_ns)
  ) {
    throw new Error("ready record mismatch");
  }
}

export function validateInstallSmokeEvidence(value) {
  exactKeys(
    value,
    [
      "schema_version",
      "commit_sha",
      "target",
      "node_version",
      "packages",
      "ready",
      "checks",
    ],
    "install evidence",
  );
  if (
    value.schema_version !== 1 ||
    !fullSha.test(value.commit_sha) ||
    !Object.hasOwn(CI_TARGETS, value.target) ||
    !nodeVersion.test(value.node_version) ||
    !Array.isArray(value.packages) ||
    value.packages.length !== 2
  ) {
    throw new Error("install evidence mismatch");
  }
  exactKeys(value.ready, ["first", "second", "reused"], "ready evidence");
  validateReadyRecord(value.ready.first);
  validateReadyRecord(value.ready.second);
  if (
    value.ready.reused !== true ||
    JSON.stringify(value.ready.first) !== JSON.stringify(value.ready.second)
  ) {
    throw new Error("ready evidence mismatch");
  }
  exactKeys(value.checks, INSTALL_CHECK_NAMES, "install checks");
  if (INSTALL_CHECK_NAMES.some((name) => value.checks[name] !== true)) {
    throw new Error("install checks must all pass");
  }
  return value;
}

function validatePackage(value, target, index, version, includesVersion) {
  exactKeys(
    value,
    [
      "name",
      ...(includesVersion ? ["version"] : []),
      "filename",
      "size",
      "sha256",
      "integrity",
    ],
    "package record",
  );
  const platform = index === 0;
  const expectedName = platform
    ? `@aiz.im/aizim-${target}`
    : "@aiz.im/aizim";
  const expectedFilename = platform
    ? `aiz.im-aizim-${target}-${version}.tgz`
    : `aiz.im-aizim-${version}.tgz`;
  if (
    value.name !== expectedName ||
    value.filename !== expectedFilename ||
    basename(value.filename) !== value.filename ||
    (includesVersion && value.version !== version) ||
    !Number.isSafeInteger(value.size) ||
    value.size < 1 ||
    !/^[0-9a-f]{64}$/u.test(value.sha256) ||
    !/^sha512-[A-Za-z0-9+/]+={0,2}$/u.test(value.integrity)
  ) {
    throw new Error("package record mismatch");
  }
}

function packagesMatch(packages, installPackages) {
  return packages.every((record, index) => {
    const installed = installPackages[index];
    return ["name", "filename", "size", "sha256", "integrity"].every(
      (key) => record[key] === installed[key],
    );
  });
}

function validateTestSummary(value, commit, target) {
  exactKeys(
    value,
    ["schema_version", "commit_sha", "target", "checks"],
    "test summary",
  );
  exactKeys(
    value.checks,
    ["node", "rust", "python", "install_smoke", "package_check"],
    "test summary checks",
  );
  if (
    value.schema_version !== 1 ||
    value.commit_sha !== commit ||
    value.target !== target ||
    Object.values(value.checks).some((check) => check !== true)
  ) {
    throw new Error("test summary mismatch");
  }
}

export function createCiEvidenceDocument({
  target,
  commit,
  version,
  build,
  pack,
  install,
  testSummary,
  minimumNodeVersion,
  liveNodeVersion,
}) {
  if (
    !Object.hasOwn(CI_TARGETS, target) ||
    !fullSha.test(commit) ||
    typeof version !== "string" ||
    build.schema_version !== 1 ||
    build.git_sha !== commit ||
    build.target !== target ||
    build.dirty_tracked_inputs !== false ||
    pack.schema_version !== 1 ||
    pack.git_sha !== commit ||
    pack.target !== target ||
    !Array.isArray(pack.packages) ||
    pack.packages.length !== 2
  ) {
    throw new Error("CI summary mismatch");
  }
  const checkedInstall = validateInstallSmokeEvidence(install);
  if (
    checkedInstall.commit_sha !== commit ||
    checkedInstall.target !== target ||
    checkedInstall.node_version !== liveNodeVersion
  ) {
    throw new Error("install evidence mismatch");
  }
  pack.packages.forEach((record, index) =>
    validatePackage(record, target, index, version, true),
  );
  checkedInstall.packages.forEach((record, index) =>
    validatePackage(record, target, index, version, false),
  );
  if (!packagesMatch(pack.packages, checkedInstall.packages)) {
    throw new Error("package evidence mismatch");
  }

  const minimum = minimumNodeVersion !== undefined;
  if (minimum) {
    if (
      target !== "linux-x64" ||
      minimumNodeVersion !== liveNodeVersion
    ) {
      throw new Error("minimum Node evidence mismatch");
    }
  } else {
    validateTestSummary(testSummary, commit, target);
  }

  return {
    schema_version: 1,
    commit_sha: commit,
    version,
    target,
    ...(minimum ? { minimum_node_version: minimumNodeVersion } : {}),
    dirty_tracked_inputs: build.dirty_tracked_inputs,
    restored_build_cache: false,
    runner: CI_TARGETS[target],
    packages: pack.packages.map(
      ({ name, filename, size, sha256, integrity }) => ({
        name,
        filename,
        size,
        sha256,
        integrity,
      }),
    ),
    checks: {
      source_build: true,
      ...(!minimum ? { npm_test: true } : {}),
      ...checkedInstall.checks,
    },
  };
}

async function hashFile(path, algorithm, encoding) {
  const hash = createHash(algorithm);
  for await (const chunk of createReadStream(path)) {
    hash.update(chunk);
  }
  return hash.digest(encoding);
}

async function verifyPackedFiles(pack) {
  for (const record of pack.packages) {
    const path = join(npmDistRoot, record.filename);
    const metadata = await stat(path);
    const [sha256, sha512] = await Promise.all([
      hashFile(path, "sha256", "hex"),
      hashFile(path, "sha512", "base64"),
    ]);
    if (
      !metadata.isFile() ||
      metadata.size !== record.size ||
      sha256 !== record.sha256 ||
      `sha512-${sha512}` !== record.integrity
    ) {
      throw new Error("packed artifact mismatch");
    }
  }
}

async function writePrivateJson(path, value) {
  if (!isAbsolute(path)) {
    throw new Error("CI evidence output must be absolute");
  }
  const output = resolve(path);
  await mkdir(dirname(output), { mode: 0o700, recursive: true });
  const temporary = `${output}.tmp-${process.pid}`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    mode: 0o600,
  });
  await rename(temporary, output);
}

export async function writeCiEvidence({
  target,
  minimumNodeVersion,
  output,
}) {
  const liveTarget = detectTarget();
  if (liveTarget.id !== target) {
    throw new Error("requested target does not match runner");
  }
  const build = await verifyFreshBuild();
  const [pack, install, testSummary, manifest, commit] = await Promise.all([
    readJson(packSummaryPath, "pack summary"),
    readJson(installEvidencePath, "install evidence"),
    minimumNodeVersion === undefined
      ? readJson(testSummaryPath, "test summary")
      : Promise.resolve(undefined),
    readJson(join(repositoryRoot, "package.json"), "package manifest"),
    capture("git", ["rev-parse", "HEAD"], { cwd: repositoryRoot }),
  ]);
  await verifyPackedFiles(pack);
  const document = createCiEvidenceDocument({
    target,
    commit,
    version: manifest.version,
    build,
    pack,
    install,
    testSummary,
    minimumNodeVersion,
    liveNodeVersion: process.version,
  });
  await writePrivateJson(output, document);
  return document;
}

function parseArguments(arguments_) {
  const values = {};
  for (let index = 0; index < arguments_.length; index += 2) {
    const flag = arguments_[index];
    const value = arguments_[index + 1];
    if (
      !["--target", "--minimum-node-version", "--output"].includes(flag) ||
      typeof value !== "string" ||
      value.startsWith("--") ||
      Object.hasOwn(values, flag)
    ) {
      throw new Error("invalid CI evidence arguments");
    }
    values[flag] = value;
  }
  if (
    arguments_.length % 2 !== 0 ||
    !Object.hasOwn(values, "--target") ||
    !Object.hasOwn(values, "--output")
  ) {
    throw new Error("invalid CI evidence arguments");
  }
  return values;
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : undefined;
if (invokedPath === fileURLToPath(import.meta.url)) {
  const values = parseArguments(process.argv.slice(2));
  await writeCiEvidence({
    target: values["--target"],
    minimumNodeVersion: values["--minimum-node-version"],
    output: values["--output"],
  });
  process.stdout.write(`${values["--output"]}\n`);
}
