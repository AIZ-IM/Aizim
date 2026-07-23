import { spawn } from "node:child_process";
import {
  mkdir,
  rename,
  writeFile,
} from "node:fs/promises";
import { dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const packageName =
  /^@aiz\.im\/aizim(?:-(?:darwin|linux)-(?:arm64|x64))?$/u;
const semver = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/u;
const integrityPattern = /^sha512-[A-Za-z0-9+/]+={0,2}$/u;

export class RegistryNotFoundError extends Error {
  constructor() {
    super("registry package version not found");
    this.name = "RegistryNotFoundError";
  }
}

export class RegistryMismatchError extends Error {
  constructor() {
    super("registry package version or integrity mismatch");
    this.name = "RegistryMismatchError";
  }
}

async function runNpmView(name, version) {
  return await new Promise((resolvePromise, reject) => {
    const child = spawn(
      "npm",
      [
        "view",
        `${name}@${version}`,
        "version",
        "dist.integrity",
        "--json",
      ],
      {
        shell: false,
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.once("error", () =>
      reject(new Error("npm view failed to start")),
    );
    child.once("exit", (code) =>
      resolvePromise({
        code: Number.isInteger(code) ? code : 70,
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
      }),
    );
  });
}

const waitFiveSeconds = async (milliseconds) =>
  await new Promise((resolvePromise) =>
    setTimeout(resolvePromise, milliseconds),
  );

function registryRecord(stdout) {
  let value;
  try {
    value = JSON.parse(stdout);
  } catch {
    throw new RegistryMismatchError();
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new RegistryMismatchError();
  }
  return {
    version: value.version,
    integrity: value["dist.integrity"] ?? value.dist?.integrity,
  };
}

async function writeReceipt(output, value) {
  if (!isAbsolute(output)) {
    throw new Error("registry receipt output must be absolute");
  }
  const path = resolve(output);
  await mkdir(dirname(path), { mode: 0o700, recursive: true });
  const temporary = `${path}.tmp-${process.pid}`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    mode: 0o600,
  });
  await rename(temporary, path);
}

export async function verifyRegistryPackage({
  name,
  version,
  integrity,
  output,
  wait = false,
  runNpm = runNpmView,
  delay = waitFiveSeconds,
}) {
  if (
    !packageName.test(name) ||
    !semver.test(version) ||
    !integrityPattern.test(integrity) ||
    (output !== undefined && !isAbsolute(output))
  ) {
    throw new Error("invalid registry verification arguments");
  }
  const maximumAttempts = wait ? 12 : 1;
  for (let attempt = 1; attempt <= maximumAttempts; attempt += 1) {
    const result = await runNpm(name, version);
    if (result.code === 0) {
      const record = registryRecord(result.stdout);
      if (
        record.version !== version ||
        record.integrity !== integrity
      ) {
        throw new RegistryMismatchError();
      }
      const receipt = {
        schema_version: 1,
        name,
        version,
        integrity,
        checks: {
          exact_version: true,
          exact_integrity: true,
        },
      };
      if (output !== undefined) {
        await writeReceipt(output, receipt);
      }
      return receipt;
    }
    if (!/\bE404\b/u.test(result.stderr)) {
      throw new Error("npm registry query failed");
    }
    if (attempt === maximumAttempts) {
      throw new RegistryNotFoundError();
    }
    await delay(5_000);
  }
  throw new RegistryNotFoundError();
}

function parseArguments(arguments_) {
  const values = {};
  let wait = false;
  for (let index = 0; index < arguments_.length; index += 1) {
    const flag = arguments_[index];
    if (flag === "--wait") {
      if (wait) {
        throw new Error("invalid registry verification arguments");
      }
      wait = true;
      continue;
    }
    const value = arguments_[index + 1];
    if (
      !["--name", "--version", "--integrity", "--output"].includes(flag) ||
      typeof value !== "string" ||
      value.startsWith("--") ||
      Object.hasOwn(values, flag)
    ) {
      throw new Error("invalid registry verification arguments");
    }
    values[flag] = value;
    index += 1;
  }
  if (
    ["--name", "--version", "--integrity", "--output"].some(
      (flag) => !Object.hasOwn(values, flag),
    )
  ) {
    throw new Error("invalid registry verification arguments");
  }
  return { values, wait };
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : undefined;
if (invokedPath === fileURLToPath(import.meta.url)) {
  const parsed = parseArguments(process.argv.slice(2));
  await verifyRegistryPackage({
    name: parsed.values["--name"],
    version: parsed.values["--version"],
    integrity: parsed.values["--integrity"],
    output: parsed.values["--output"],
    wait: parsed.wait,
  });
  process.stdout.write(`${parsed.values["--output"]}\n`);
}
