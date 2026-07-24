import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import {
  lstat,
  mkdir,
  readFile,
  readdir,
  rename,
  writeFile,
} from "node:fs/promises";
import {
  basename,
  dirname,
  isAbsolute,
  relative,
  resolve,
  sep,
} from "node:path";
import { fileURLToPath } from "node:url";

export const CI_TARGETS = Object.freeze({
  "darwin-arm64": Object.freeze({
    os: "darwin",
    arch: "arm64",
    libc: null,
  }),
  "linux-arm64": Object.freeze({
    os: "linux",
    arch: "arm64",
    libc: "glibc",
  }),
  "linux-x64": Object.freeze({
    os: "linux",
    arch: "x64",
    libc: "glibc",
  }),
});

export const INSTALL_CHECK_NAMES = Object.freeze([
  "local_install",
  "global_install",
  "npx_no_install",
  "python_314_bootstrap",
  "cache_reused",
  "uninstall_preserved_cache",
  "local_codex_01450",
  "global_codex_01450",
  "local_claude_21218",
  "global_claude_21218",
  "ready",
  "security_gate",
  "aizim_run",
  "controller_loop",
  "missing_platform_exit_78",
  "integrity_failure_exit_74",
]);

const nativeCheckNames = Object.freeze([
  "source_build",
  "npm_test",
  ...INSTALL_CHECK_NAMES,
]);
const minimumCheckNames = Object.freeze([
  "source_build",
  ...INSTALL_CHECK_NAMES,
]);
const fullSha = /^[0-9a-f]{40}$/u;
const packageSha = /^[0-9a-f]{64}$/u;
const packageIntegrity = /^sha512-[A-Za-z0-9+/]+={0,2}$/u;
const semver = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/u;

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

function containedBy(root, candidate) {
  const fromRoot = relative(root, candidate);
  return (
    fromRoot === "" ||
    (!isAbsolute(fromRoot) &&
      fromRoot !== ".." &&
      !fromRoot.startsWith(`..${sep}`))
  );
}

async function hashFile(path, algorithm, encoding) {
  const hash = createHash(algorithm);
  for await (const chunk of createReadStream(path)) {
    hash.update(chunk);
  }
  return hash.digest(encoding);
}

async function readJson(path) {
  let value;
  try {
    value = JSON.parse(await readFile(path, "utf8"));
  } catch {
    throw new Error("invalid evidence JSON");
  }
  return object(value, "evidence");
}

function validateRunner(value, target) {
  exactKeys(value, ["os", "arch", "libc"], "runner");
  const expected = CI_TARGETS[target];
  if (
    !expected ||
    value.os !== expected.os ||
    value.arch !== expected.arch ||
    value.libc !== expected.libc
  ) {
    throw new Error("runner mismatch");
  }
}

function validateChecks(value, expected) {
  exactKeys(value, expected, "evidence checks");
  if (expected.some((name) => value[name] !== true)) {
    throw new Error("evidence checks must all pass");
  }
}

function validatePackageRecord(value, target, index, version) {
  exactKeys(
    value,
    ["name", "filename", "size", "sha256", "integrity"],
    "package",
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
    basename(value.filename) !== value.filename
  ) {
    throw new Error("package filename or name mismatch");
  }
  if (
    !Number.isSafeInteger(value.size) ||
    value.size < 1 ||
    typeof value.sha256 !== "string" ||
    !packageSha.test(value.sha256) ||
    typeof value.integrity !== "string" ||
    !packageIntegrity.test(value.integrity)
  ) {
    throw new Error("package metadata mismatch");
  }
}

function validateDocument(value, { commit, version, minimum }) {
  const fields = [
    "schema_version",
    "commit_sha",
    "version",
    "target",
    ...(minimum ? ["minimum_node_version"] : []),
    "dirty_tracked_inputs",
    "restored_build_cache",
    "runner",
    "packages",
    "checks",
  ];
  exactKeys(value, fields, "evidence");
  if (value.schema_version !== 1) {
    throw new Error("evidence schema mismatch");
  }
  if (!fullSha.test(value.commit_sha) || value.commit_sha !== commit) {
    throw new Error("commit mismatch");
  }
  if (!semver.test(value.version) || value.version !== version) {
    throw new Error("version mismatch");
  }
  if (
    value.dirty_tracked_inputs !== false ||
    value.restored_build_cache !== false
  ) {
    throw new Error("build provenance mismatch");
  }
  if (!Object.hasOwn(CI_TARGETS, value.target)) {
    throw new Error("unknown target");
  }
  validateRunner(value.runner, value.target);
  if (!Array.isArray(value.packages) || value.packages.length !== 2) {
    throw new Error("package records mismatch");
  }
  value.packages.forEach((record, index) =>
    validatePackageRecord(record, value.target, index, value.version),
  );
  validateChecks(
    value.checks,
    minimum ? minimumCheckNames : nativeCheckNames,
  );
  return value;
}

export function validateCiEvidenceDocument(value, options) {
  return validateDocument(value, options);
}

async function verifyPackage(directory, root, record) {
  const path = resolve(directory, record.filename);
  if (!containedBy(root, path) || basename(path) !== record.filename) {
    throw new Error("package filename escapes artifact root");
  }
  let metadata;
  try {
    metadata = await lstat(path);
  } catch {
    throw new Error("package file is missing");
  }
  if (
    !metadata.isFile() ||
    metadata.isSymbolicLink() ||
    metadata.size !== record.size
  ) {
    throw new Error("package size mismatch");
  }
  const [sha256, sha512] = await Promise.all([
    hashFile(path, "sha256", "hex"),
    hashFile(path, "sha512", "base64"),
  ]);
  if (
    sha256 !== record.sha256 ||
    `sha512-${sha512}` !== record.integrity
  ) {
    throw new Error("package digest mismatch");
  }
}

export async function verifyCiEvidencePackage(directory, root, record) {
  await verifyPackage(directory, root, record);
}

async function collectEvidenceFiles(root) {
  const native = [];
  const minimum = [];

  async function visit(path) {
    const metadata = await lstat(path);
    if (metadata.isSymbolicLink()) {
      throw new Error("artifact root contains a symlink");
    }
    if (metadata.isFile()) {
      const name = basename(path);
      if (/^native-evidence-.+\.json$/u.test(name)) {
        native.push(path);
      } else if (name === "minimum-node-evidence.json") {
        minimum.push(path);
      }
      return;
    }
    if (!metadata.isDirectory()) {
      throw new Error("artifact root contains an unsupported entry");
    }
    const entries = await readdir(path);
    for (const entry of entries.sort()) {
      await visit(resolve(path, entry));
    }
  }

  await visit(root);
  return { minimum, native };
}

async function writePrivateJson(path, value, root) {
  const output = resolve(path);
  if (!isAbsolute(path) || !containedBy(root, output)) {
    throw new Error("aggregate output must be inside artifact root");
  }
  await mkdir(dirname(output), { mode: 0o700, recursive: true });
  const temporary = `${output}.tmp-${process.pid}`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    mode: 0o600,
  });
  await rename(temporary, output);
}

export async function verifyEvidenceDirectory({
  directory,
  commit,
  version,
  minimumNodeVersion,
  output,
}) {
  if (
    !isAbsolute(directory) ||
    !fullSha.test(commit) ||
    !semver.test(version) ||
    typeof minimumNodeVersion !== "string" ||
    !/^v\d+\.\d+\.\d+$/u.test(minimumNodeVersion) ||
    !isAbsolute(output)
  ) {
    throw new Error("invalid aggregate arguments");
  }
  const root = resolve(directory);
  const metadata = await lstat(root);
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
    throw new Error("artifact root is invalid");
  }
  const files = await collectEvidenceFiles(root);
  if (
    files.native.length !== Object.keys(CI_TARGETS).length ||
    files.minimum.length !== 1
  ) {
    throw new Error("evidence target set is incomplete");
  }

  const documents = [];
  const seen = new Set();
  for (const path of files.native) {
    const raw = await readJson(path);
    if (seen.has(raw.target)) {
      throw new Error("duplicate target");
    }
    seen.add(raw.target);
    const document = validateDocument(raw, {
      commit,
      version,
      minimum: false,
    });
    for (const record of document.packages) {
      await verifyPackage(dirname(path), root, record);
    }
    documents.push(document);
  }
  const expectedTargets = Object.keys(CI_TARGETS).sort();
  if (
    seen.size !== expectedTargets.length ||
    expectedTargets.some((target) => !seen.has(target))
  ) {
    throw new Error("evidence target set is incomplete");
  }

  const minimum = validateDocument(await readJson(files.minimum[0]), {
    commit,
    version,
    minimum: true,
  });
  if (
    minimum.target !== "linux-x64" ||
    minimum.minimum_node_version !== minimumNodeVersion
  ) {
    throw new Error("minimum Node evidence mismatch");
  }
  for (const record of minimum.packages) {
    await verifyPackage(dirname(files.minimum[0]), root, record);
  }

  const metaRecords = [
    ...documents.map((document) => document.packages[1]),
    minimum.packages[1],
  ];
  const commonMeta = metaRecords[0];
  if (
    metaRecords.some(
      (record) =>
        record.sha256 !== commonMeta.sha256 ||
        record.integrity !== commonMeta.integrity ||
        record.size !== commonMeta.size,
    )
  ) {
    throw new Error("meta package mismatch");
  }

  const aggregate = {
    schema_version: 1,
    commit_sha: commit,
    version,
    minimum_node_version: minimumNodeVersion,
    targets: expectedTargets,
    meta: {
      size: commonMeta.size,
      sha256: commonMeta.sha256,
      integrity: commonMeta.integrity,
    },
    platforms: documents
      .map((document) => ({
        target: document.target,
        size: document.packages[0].size,
        sha256: document.packages[0].sha256,
        integrity: document.packages[0].integrity,
      }))
      .sort((left, right) => left.target.localeCompare(right.target)),
  };
  await writePrivateJson(output, aggregate, root);
  return aggregate;
}

function parseArguments(arguments_) {
  const allowed = new Set([
    "--directory",
    "--commit",
    "--version",
    "--minimum-node-version",
    "--output",
  ]);
  const values = {};
  for (let index = 0; index < arguments_.length; index += 2) {
    const flag = arguments_[index];
    const value = arguments_[index + 1];
    if (
      !allowed.has(flag) ||
      typeof value !== "string" ||
      value.startsWith("--") ||
      Object.hasOwn(values, flag)
    ) {
      throw new Error("invalid aggregate arguments");
    }
    values[flag] = value;
  }
  if (
    arguments_.length !== allowed.size * 2 ||
    [...allowed].some((flag) => !Object.hasOwn(values, flag))
  ) {
    throw new Error("invalid aggregate arguments");
  }
  return values;
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : undefined;
if (invokedPath === fileURLToPath(import.meta.url)) {
  const values = parseArguments(process.argv.slice(2));
  await verifyEvidenceDirectory({
    directory: values["--directory"],
    commit: values["--commit"],
    version: values["--version"],
    minimumNodeVersion: values["--minimum-node-version"],
    output: values["--output"],
  });
  process.stdout.write(`${values["--output"]}\n`);
}
