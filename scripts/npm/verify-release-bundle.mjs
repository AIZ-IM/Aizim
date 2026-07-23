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
  join,
  relative,
  resolve,
  sep,
} from "node:path";
import { fileURLToPath } from "node:url";

import {
  CI_TARGETS,
  validateCiEvidenceDocument,
  verifyCiEvidencePackage,
} from "./verify-ci-evidence.mjs";
import { repositoryRoot } from "./lib/paths.mjs";

const fullSha = /^[0-9a-f]{40}$/u;
const semver = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/u;

function containedBy(root, candidate) {
  const fromRoot = relative(root, candidate);
  return (
    fromRoot === "" ||
    (!isAbsolute(fromRoot) &&
      fromRoot !== ".." &&
      !fromRoot.startsWith(`..${sep}`))
  );
}

function sectionVersion(text, section, label) {
  const escaped = section.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
  const header = new RegExp(`^\\[${escaped}\\]\\s*$`, "mu").exec(text);
  if (!header) {
    throw new Error(`${label} version is missing`);
  }
  const bodyStart = text.indexOf("\n", header.index + header[0].length);
  const remainder = bodyStart === -1 ? "" : text.slice(bodyStart + 1);
  const nextSection = /^\[/mu.exec(remainder);
  const body =
    nextSection === null
      ? remainder
      : remainder.slice(0, nextSection.index);
  const version = body.match(
    /^version\s*=\s*"([^"]+)"\s*$/mu,
  )?.[1];
  if (!version) {
    throw new Error(`${label} version is missing`);
  }
  return version;
}

export function validateVersionSet(values, expectedVersion) {
  if (
    !semver.test(expectedVersion) ||
    values === null ||
    typeof values !== "object" ||
    Array.isArray(values) ||
    values.python !== expectedVersion ||
    values.npm !== expectedVersion ||
    values.cargo !== expectedVersion ||
    values.platforms === null ||
    typeof values.platforms !== "object" ||
    Array.isArray(values.platforms)
  ) {
    throw new Error("source version mismatch");
  }
  const targets = Object.keys(CI_TARGETS).sort();
  const actualTargets = Object.keys(values.platforms).sort();
  if (
    actualTargets.length !== targets.length ||
    targets.some(
      (target, index) =>
        actualTargets[index] !== target ||
        values.platforms[target] !== expectedVersion,
    )
  ) {
    throw new Error("platform version mismatch");
  }
  return values;
}

export async function readSourceVersions() {
  const [pythonText, cargoText, npmText, ...platformTexts] =
    await Promise.all([
      readFile(join(repositoryRoot, "pyproject.toml"), "utf8"),
      readFile(join(repositoryRoot, "Cargo.toml"), "utf8"),
      readFile(join(repositoryRoot, "package.json"), "utf8"),
      ...Object.keys(CI_TARGETS).flatMap((target) => [
        readFile(
          join(repositoryRoot, "npm", "platforms", target, "package.json"),
          "utf8",
        ),
        readFile(
          join(
            repositoryRoot,
            "npm",
            "platforms",
            target,
            "package.publish.json",
          ),
          "utf8",
        ),
      ]),
    ]);
  const targets = Object.keys(CI_TARGETS);
  const platforms = {};
  for (const [index, target] of targets.entries()) {
    const source = JSON.parse(platformTexts[index * 2]).version;
    const published = JSON.parse(platformTexts[index * 2 + 1]).version;
    if (source !== published) {
      throw new Error("platform source and publish version mismatch");
    }
    platforms[target] = source;
  }
  return {
    python: sectionVersion(pythonText, "project", "Python"),
    cargo: sectionVersion(cargoText, "workspace.package", "Cargo"),
    npm: JSON.parse(npmText).version,
    platforms,
  };
}

async function collectBundle(root) {
  const evidence = [];
  const otherFiles = [];
  const tarballs = [];

  async function visit(path) {
    const metadata = await lstat(path);
    if (metadata.isSymbolicLink()) {
      throw new Error("release bundle contains a symlink");
    }
    if (metadata.isFile()) {
      const name = basename(path);
      if (/^native-evidence-.+\.json$/u.test(name)) {
        evidence.push(path);
      } else if (name.endsWith(".tgz")) {
        tarballs.push(path);
      } else {
        otherFiles.push(path);
      }
      return;
    }
    if (!metadata.isDirectory()) {
      throw new Error("release bundle contains an unsupported entry");
    }
    for (const entry of (await readdir(path)).sort()) {
      await visit(join(path, entry));
    }
  }

  await visit(root);
  return { evidence, otherFiles, tarballs };
}

function expectedTarballNames(version) {
  return [
    `aiz.im-aizim-${version}.tgz`,
    ...Object.keys(CI_TARGETS).map(
      (target) => `aiz.im-aizim-${target}-${version}.tgz`,
    ),
  ].sort();
}

async function writePrivateJson(path, value, root) {
  const output = resolve(path);
  if (!isAbsolute(path) || !containedBy(root, output)) {
    throw new Error("release aggregate output must be inside bundle root");
  }
  await mkdir(dirname(output), { mode: 0o700, recursive: true });
  const temporary = `${output}.tmp-${process.pid}`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    mode: 0o600,
  });
  await rename(temporary, output);
}

export async function verifyReleaseBundle({
  directory,
  commit,
  version,
  output,
  sourceVersions,
}) {
  if (
    !isAbsolute(directory) ||
    !isAbsolute(output) ||
    !fullSha.test(commit) ||
    !semver.test(version)
  ) {
    throw new Error("invalid release bundle arguments");
  }
  const root = resolve(directory);
  const metadata = await lstat(root);
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
    throw new Error("release bundle root is invalid");
  }
  validateVersionSet(sourceVersions ?? (await readSourceVersions()), version);

  const bundle = await collectBundle(root);
  const resolvedOutput = resolve(output);
  if (
    bundle.otherFiles.some((path) => resolve(path) !== resolvedOutput)
  ) {
    throw new Error("release bundle contains an unexpected file");
  }
  if (bundle.evidence.length !== 4 || bundle.tarballs.length !== 5) {
    throw new Error("release bundle file set mismatch");
  }
  const tarballNames = bundle.tarballs.map((path) => basename(path)).sort();
  const wantedTarballs = expectedTarballNames(version);
  if (
    tarballNames.some((name, index) => name !== wantedTarballs[index])
  ) {
    throw new Error("release bundle tarball set mismatch");
  }

  const documents = [];
  const seen = new Set();
  for (const path of bundle.evidence) {
    const raw = JSON.parse(await readFile(path, "utf8"));
    if (raw.dirty_tracked_inputs !== false) {
      throw new Error("release evidence reports dirty tracked inputs");
    }
    if (raw.restored_build_cache !== false) {
      throw new Error("release evidence reports restored build cache");
    }
    const document = validateCiEvidenceDocument(
      raw,
      { commit, version, minimum: false },
    );
    if (seen.has(document.target)) {
      throw new Error("duplicate release target");
    }
    seen.add(document.target);
    for (const record of document.packages) {
      await verifyCiEvidencePackage(root, root, record);
    }
    documents.push(document);
  }

  const targets = Object.keys(CI_TARGETS).sort();
  if (
    seen.size !== targets.length ||
    targets.some((target) => !seen.has(target))
  ) {
    throw new Error("release target set mismatch");
  }
  const metaRecords = documents.map((document) => document.packages[1]);
  const meta = metaRecords[0];
  if (
    metaRecords.some(
      (record) =>
        record.size !== meta.size ||
        record.sha256 !== meta.sha256 ||
        record.integrity !== meta.integrity,
    )
  ) {
    throw new Error("release meta package mismatch");
  }

  const aggregate = {
    schema_version: 1,
    commit_sha: commit,
    version,
    targets,
    dirty_tracked_inputs: false,
    restored_build_cache: false,
    meta: {
      size: meta.size,
      sha256: meta.sha256,
      integrity: meta.integrity,
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
      throw new Error("invalid release bundle arguments");
    }
    values[flag] = value;
  }
  if (
    arguments_.length !== allowed.size * 2 ||
    [...allowed].some((flag) => !Object.hasOwn(values, flag))
  ) {
    throw new Error("invalid release bundle arguments");
  }
  return values;
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : undefined;
if (invokedPath === fileURLToPath(import.meta.url)) {
  const values = parseArguments(process.argv.slice(2));
  await verifyReleaseBundle({
    directory: values["--directory"],
    commit: values["--commit"],
    version: values["--version"],
    output: values["--output"],
  });
  process.stdout.write(`${values["--output"]}\n`);
}
