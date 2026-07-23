import {
  lstat,
  mkdir,
  readFile,
  readdir,
  rename,
  writeFile,
} from "node:fs/promises";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { CI_TARGETS } from "./verify-ci-evidence.mjs";

const packageNames = [
  "@aiz.im/aizim",
  ...Object.keys(CI_TARGETS).map((target) => `@aiz.im/aizim-${target}`),
].sort();
const fullSha = /^[0-9a-f]{40}$/u;
const semver = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/u;

function validatedInput({ aggregate, receipts, smoke, runIds }) {
  if (
    aggregate?.schema_version !== 1 ||
    !fullSha.test(aggregate.commit_sha) ||
    !semver.test(aggregate.version) ||
    !Array.isArray(aggregate.targets) ||
    JSON.stringify(aggregate.targets) !==
      JSON.stringify(Object.keys(CI_TARGETS).sort()) ||
    !Array.isArray(aggregate.platforms) ||
    aggregate.platforms.length !== Object.keys(CI_TARGETS).length ||
    !Array.isArray(receipts) ||
    receipts.length !== packageNames.length ||
    smoke?.schema_version !== 1 ||
    smoke.version !== aggregate.version ||
    JSON.stringify(smoke.targets) !==
      JSON.stringify(Object.keys(CI_TARGETS).sort()) ||
    !/^\d+$/u.test(runIds?.release) ||
    !/^\d+$/u.test(runIds?.registry_smoke)
  ) {
    throw new Error("release record input mismatch");
  }
  const sortedReceipts = [...receipts].sort((left, right) =>
    left.name.localeCompare(right.name),
  );
  if (
    sortedReceipts.some(
      (receipt, index) =>
        receipt.name !== packageNames[index] ||
        receipt.version !== aggregate.version ||
        typeof receipt.integrity !== "string",
    )
  ) {
    throw new Error("release receipt mismatch");
  }
  const expectedIntegrities = new Map([
    ["@aiz.im/aizim", aggregate.meta.integrity],
    ...aggregate.platforms.map((platform) => [
      `@aiz.im/aizim-${platform.target}`,
      platform.integrity,
    ]),
  ]);
  if (
    sortedReceipts.some(
      (receipt) =>
        receipt.integrity !== expectedIntegrities.get(receipt.name),
    )
  ) {
    throw new Error("release receipt integrity mismatch");
  }
  return sortedReceipts;
}

export function renderReleaseRecord(input) {
  const receipts = validatedInput(input);
  const lines = [
    `# npm ${input.aggregate.version}`,
    "",
    `Git commit: \`${input.aggregate.commit_sha}\``,
    `Release workflow run: \`${input.runIds.release}\``,
    `Registry smoke workflow run: \`${input.runIds.registry_smoke}\``,
    "",
    "## Registry packages",
    "",
    "| Package | Integrity |",
    "| --- | --- |",
    ...receipts.map(
      (receipt) => `| \`${receipt.name}\` | \`${receipt.integrity}\` |`,
    ),
    "",
    "## Verification",
    "",
    `Targets: ${input.smoke.targets.map((target) => `\`${target}\``).join(", ")}`,
    "",
    "The source repository had private repository provenance at publication time.",
    "The automated release and registry smoke used no credentialed model run.",
    "",
  ];
  return lines.join("\n");
}

async function collectReceipts(root) {
  const receipts = [];
  async function visit(path) {
    const metadata = await lstat(path);
    if (metadata.isSymbolicLink()) {
      throw new Error("release receipts contain a symlink");
    }
    if (metadata.isFile()) {
      if (/^registry-receipt-.+\.json$/u.test(basename(path))) {
        receipts.push(JSON.parse(await readFile(path, "utf8")));
      }
      return;
    }
    if (!metadata.isDirectory()) {
      throw new Error("release receipt artifact is invalid");
    }
    for (const entry of (await readdir(path)).sort()) {
      await visit(join(path, entry));
    }
  }
  await visit(root);
  return receipts;
}

export async function writeReleaseRecord({
  aggregatePath,
  receiptsDirectory,
  smokePath,
  releaseRunId,
  registrySmokeRunId,
  output,
}) {
  if (
    !isAbsolute(aggregatePath) ||
    !isAbsolute(receiptsDirectory) ||
    !isAbsolute(smokePath) ||
    !isAbsolute(output)
  ) {
    throw new Error("release record paths must be absolute");
  }
  const [aggregate, receipts, smoke] = await Promise.all([
    readFile(aggregatePath, "utf8").then(JSON.parse),
    collectReceipts(receiptsDirectory),
    readFile(smokePath, "utf8").then(JSON.parse),
  ]);
  const record = renderReleaseRecord({
    aggregate,
    receipts,
    smoke,
    runIds: {
      release: releaseRunId,
      registry_smoke: registrySmokeRunId,
    },
  });
  const path = resolve(output);
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.tmp-${process.pid}`;
  await writeFile(temporary, record, { mode: 0o644 });
  await rename(temporary, path);
  return record;
}

function parseArguments(arguments_) {
  const allowed = new Set([
    "--aggregate",
    "--receipts-directory",
    "--smoke",
    "--release-run-id",
    "--registry-smoke-run-id",
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
      throw new Error("invalid release record arguments");
    }
    values[flag] = value;
  }
  if (
    arguments_.length !== allowed.size * 2 ||
    [...allowed].some((flag) => !Object.hasOwn(values, flag))
  ) {
    throw new Error("invalid release record arguments");
  }
  return values;
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : undefined;
if (invokedPath === fileURLToPath(import.meta.url)) {
  const values = parseArguments(process.argv.slice(2));
  await writeReleaseRecord({
    aggregatePath: values["--aggregate"],
    receiptsDirectory: values["--receipts-directory"],
    smokePath: values["--smoke"],
    releaseRunId: values["--release-run-id"],
    registrySmokeRunId: values["--registry-smoke-run-id"],
    output: values["--output"],
  });
  process.stdout.write(`${values["--output"]}\n`);
}
