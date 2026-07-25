import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  access,
  chmod,
  cp,
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rename,
  rm,
  stat,
  symlink,
  writeFile,
} from "node:fs/promises";
import { constants } from "node:fs";
import { tmpdir } from "node:os";
import {
  basename,
  delimiter,
  dirname,
  isAbsolute,
  join,
  resolve,
} from "node:path";
import { fileURLToPath } from "node:url";

import { detectTarget } from "../../lib/platform.mjs";
import {
  assertNoAgentDependencies,
  validateProviderFreeCliEvidence,
} from "./install-smoke.mjs";
import { repositoryRoot } from "./lib/paths.mjs";
import { CI_TARGETS } from "./verify-ci-evidence.mjs";

const semver = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/u;
const checkNames = [
  "provider_free_install",
  "provider_free_cli",
  "expected_doctor_failure",
  "cache_reused",
  "lean_build",
  "aizim_run",
];
const preservedEnvironment = new Set([
  "HTTP_PROXY",
  "HTTPS_PROXY",
  "NO_PROXY",
  "SSL_CERT_DIR",
  "SSL_CERT_FILE",
]);

async function execute(program, arguments_, options) {
  return await new Promise((resolvePromise, reject) => {
    const child = spawn(program, arguments_, {
      cwd: options.cwd,
      env: options.env,
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (value) => stdout.push(value));
    child.stderr.on("data", (value) => stderr.push(value));
    child.once("error", () =>
      reject(new Error("registry smoke command failed to start")),
    );
    child.once("exit", (code, signal) =>
      resolvePromise({
        code: Number.isInteger(code) ? code : 70,
        signal,
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
      }),
    );
  });
}

async function requireSuccess(program, arguments_, options, label) {
  const result = await execute(program, arguments_, options);
  if (result.code !== 0 || result.signal !== null) {
    throw new Error(`${label} failed`);
  }
  return result;
}

async function findExecutable(name) {
  for (const directory of (process.env.PATH ?? "").split(delimiter)) {
    if (!directory) {
      continue;
    }
    const candidate = join(directory, name);
    try {
      await access(candidate, constants.X_OK);
      return candidate;
    } catch {
      continue;
    }
  }
  throw new Error(`required ${name} executable is unavailable`);
}

async function hashFile(path) {
  const value = await readFile(path);
  return createHash("sha256").update(value).digest("hex");
}

async function readyFiles(root) {
  const matches = [];
  async function visit(path) {
    for (const entry of await readdir(path, { withFileTypes: true })) {
      const child = join(path, entry.name);
      if (entry.isDirectory()) {
        await visit(child);
      } else if (entry.isFile() && entry.name === "READY.json") {
        matches.push(child);
      }
    }
  }
  await visit(root);
  return matches;
}

async function readyRecord(path) {
  const metadata = await stat(path, { bigint: true });
  return {
    sha256: await hashFile(path),
    mtime_ns: metadata.mtimeNs.toString(),
  };
}

async function copyFixture(source, destination) {
  await cp(source, destination, {
    recursive: true,
    filter: (path) => ![".aizim", ".lake"].includes(basename(path)),
  });
}

async function providerFreeTools(layout, lake) {
  await mkdir(layout.tools, { mode: 0o700 });
  await symlink(process.execPath, join(layout.tools, "node"));
  await symlink(lake, join(layout.tools, "lake"));
  for (const name of ["lean", "rg"]) {
    await symlink(await findExecutable(name), join(layout.tools, name));
  }
  for (const name of ["codex", "claude"]) {
    for (const directory of [layout.tools, "/usr/bin", "/bin"]) {
      try {
        await access(join(directory, name), constants.X_OK);
      } catch {
        continue;
      }
      throw new Error(`provider-free PATH exposes ${name}`);
    }
  }
}

function consumerEnvironment(layout) {
  const environment = Object.fromEntries(
    Object.entries(process.env).filter(([name]) =>
      preservedEnvironment.has(name),
    ),
  );
  const elanHome =
    process.env.ELAN_HOME ??
    (process.env.HOME ? join(process.env.HOME, ".elan") : undefined);
  return {
    ...environment,
    ...(elanHome ? { ELAN_HOME: elanHome } : {}),
    AIZIM_CACHE_DIR: layout.cache,
    HOME: layout.home,
    LANG: "C.UTF-8",
    LC_ALL: "C.UTF-8",
    npm_config_prefix: layout.prefix,
    PATH: [
      layout.tools,
      "/usr/bin",
      "/bin",
    ].join(delimiter),
    TMPDIR: layout.temporary,
  };
}

async function writePrivateJson(output, value) {
  if (!isAbsolute(output)) {
    throw new Error("registry smoke output must be absolute");
  }
  const path = resolve(output);
  await mkdir(dirname(path), { mode: 0o700, recursive: true });
  const temporary = `${path}.tmp-${process.pid}`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    mode: 0o600,
  });
  await rename(temporary, path);
}

function validateSmokeDocument(document, version) {
  if (
    document === null ||
    typeof document !== "object" ||
    Array.isArray(document) ||
    JSON.stringify(Object.keys(document).sort()) !==
      JSON.stringify(
        ["schema_version", "version", "target", "checks"].sort(),
      ) ||
    document.schema_version !== 1 ||
    document.version !== version ||
    !Object.hasOwn(CI_TARGETS, document.target) ||
    document.checks === null ||
    typeof document.checks !== "object" ||
    Array.isArray(document.checks) ||
    JSON.stringify(Object.keys(document.checks).sort()) !==
      JSON.stringify([...checkNames].sort()) ||
    checkNames.some((name) => document.checks[name] !== true)
  ) {
    throw new Error("registry smoke checks mismatch");
  }
  return document;
}

export function createRegistrySmokeAggregate(documents, version) {
  if (!semver.test(version) || !Array.isArray(documents)) {
    throw new Error("registry smoke arguments mismatch");
  }
  const seen = new Set();
  for (const raw of documents) {
    const document = validateSmokeDocument(raw, version);
    if (seen.has(document.target)) {
      throw new Error("registry smoke target mismatch");
    }
    seen.add(document.target);
  }
  const targets = Object.keys(CI_TARGETS).sort();
  if (
    seen.size !== targets.length ||
    targets.some((target) => !seen.has(target))
  ) {
    throw new Error("registry smoke target mismatch");
  }
  return {
    schema_version: 1,
    version,
    targets,
    checks: {
      provider_free_install: true,
      provider_free_cli: true,
      expected_doctor_failure: true,
      cache_reused: true,
      lean_build: true,
      aizim_run: true,
    },
  };
}

export async function registrySmoke({ version, output }) {
  if (!semver.test(version) || !isAbsolute(output)) {
    throw new Error("invalid registry smoke arguments");
  }
  const target = detectTarget().id;
  const generated = await mkdtemp(join(tmpdir(), "aizim-registry-smoke-"));
  await chmod(generated, 0o700);
  const metadata = await lstat(generated);
  if (
    !metadata.isDirectory() ||
    metadata.isSymbolicLink() ||
    (metadata.mode & 0o777) !== 0o700 ||
    !basename(generated).startsWith("aizim-registry-smoke-")
  ) {
    throw new Error("invalid registry smoke root");
  }
  const layout = {
    root: generated,
    local: join(generated, "consumer"),
    prefix: join(generated, "prefix"),
    cache: join(generated, "cache"),
    home: join(generated, "home"),
    project: join(generated, "project"),
    runProject: join(generated, "run-project"),
    tools: join(generated, "tools"),
    temporary: join(generated, "tmp"),
  };
  try {
    for (const path of [
      layout.local,
      layout.prefix,
      layout.cache,
      layout.home,
      layout.temporary,
    ]) {
      await mkdir(path, { mode: 0o700, recursive: true });
    }
    await writeFile(
      join(layout.local, "package.json"),
      '{"name":"aizim-registry-smoke","version":"0.0.0","private":true}\n',
      { mode: 0o600 },
    );
    const lake = await findExecutable("lake");
    await providerFreeTools(layout, lake);
    const environment = consumerEnvironment(layout);
    const npm = join(dirname(process.execPath), "npm");
    const packageSpec = `@aiz.im/aizim@${version}`;
    await requireSuccess(
      process.execPath,
      [
        npm,
        "install",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        packageSpec,
      ],
      { cwd: layout.local, env: environment },
      "local registry install",
    );
    const localBinary = join(
      layout.local,
      "node_modules",
      ".bin",
      "aizim",
    );
    const firstVersion = await requireSuccess(
      localBinary,
      ["--version"],
      { cwd: layout.local, env: environment },
      "local registry version",
    );
    if (firstVersion.stdout.trim() !== `aizim ${version}`) {
      throw new Error("registry version mismatch");
    }
    const localHelp = await requireSuccess(
      localBinary,
      ["--help"],
      { cwd: layout.local, env: environment },
      "local registry help",
    );
    assertNoAgentDependencies(
      JSON.parse(
        await readFile(
          join(
            layout.local,
            "node_modules",
            "@aiz.im",
            "aizim",
            "package.json",
          ),
          "utf8",
        ),
      ),
      "registry package",
    );
    const markers = await readyFiles(layout.cache);
    if (markers.length !== 1) {
      throw new Error("registry runtime readiness mismatch");
    }
    const firstReady = await readyRecord(markers[0]);
    await requireSuccess(
      localBinary,
      ["--version"],
      { cwd: layout.local, env: environment },
      "repeated registry version",
    );
    if (
      JSON.stringify(firstReady) !==
      JSON.stringify(await readyRecord(markers[0]))
    ) {
      throw new Error("registry runtime cache was not reused");
    }
    await requireSuccess(
      process.execPath,
      [
        npm,
        "install",
        "--global",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--prefix",
        layout.prefix,
        packageSpec,
      ],
      { cwd: layout.root, env: environment },
      "global registry install",
    );
    const globalVersion = await requireSuccess(
      join(layout.prefix, "bin", "aizim"),
      ["--version"],
      { cwd: layout.root, env: environment },
      "global registry version",
    );
    if (globalVersion.stdout.trim() !== `aizim ${version}`) {
      throw new Error("global registry version mismatch");
    }
    const globalHelp = await requireSuccess(
      join(layout.prefix, "bin", "aizim"),
      ["--help"],
      { cwd: layout.root, env: environment },
      "global registry help",
    );

    await copyFixture(
      join(repositoryRoot, "tests", "fixtures", "attack_probe_project"),
      layout.project,
    );
    await requireSuccess(
      localBinary,
      ["init", layout.project],
      { cwd: layout.local, env: environment },
      "registry project init",
    );
    const localDoctor = await execute(
      localBinary,
      ["doctor", "--project", layout.project, "--json"],
      { cwd: layout.local, env: environment },
    );
    validateProviderFreeCliEvidence(
      { version: firstVersion, help: localHelp, doctor: localDoctor },
      "local registry provider-free CLI",
      version,
    );
    validateProviderFreeCliEvidence(
      {
        version: globalVersion,
        help: globalHelp,
        doctor: await execute(
          join(layout.prefix, "bin", "aizim"),
          ["doctor", "--project", layout.project, "--json"],
          { cwd: layout.root, env: environment },
        ),
      },
      "global registry provider-free CLI",
      version,
    );

    await copyFixture(
      join(repositoryRoot, "examples", "smoke_lean"),
      layout.runProject,
    );
    await requireSuccess(
      lake,
      ["build"],
      { cwd: layout.runProject, env: environment },
      "registry Lean build",
    );
    await requireSuccess(
      localBinary,
      ["init", layout.runProject],
      { cwd: layout.local, env: environment },
      "registry run init",
    );
    const run = await requireSuccess(
      localBinary,
      [
        "run",
        "--project",
        layout.runProject,
        "--profile",
        "autonomous-shared",
        "--backend",
        "fake",
      ],
      { cwd: layout.local, env: environment },
      "registry deterministic run",
    );
    if (!run.stdout.includes("AIZIM RUN PASS")) {
      throw new Error("registry run mismatch");
    }

    const evidence = {
      schema_version: 1,
      version,
      target,
      checks: {
        provider_free_install: true,
        provider_free_cli: true,
        expected_doctor_failure: true,
        cache_reused: true,
        lean_build: true,
        aizim_run: true,
      },
    };
    validateSmokeDocument(evidence, version);
    await writePrivateJson(output, evidence);
    return evidence;
  } finally {
    await rm(generated, { force: true, recursive: true });
  }
}

export async function aggregateRegistrySmoke({
  directory,
  version,
  output,
}) {
  if (
    !isAbsolute(directory) ||
    !semver.test(version) ||
    !isAbsolute(output)
  ) {
    throw new Error("invalid registry smoke aggregate arguments");
  }
  const root = resolve(directory);
  const documents = [];
  async function visit(path) {
    const metadata = await lstat(path);
    if (metadata.isSymbolicLink()) {
      throw new Error("registry smoke artifact contains a symlink");
    }
    if (metadata.isFile()) {
      if (/^registry-smoke-.+\.json$/u.test(basename(path))) {
        documents.push(JSON.parse(await readFile(path, "utf8")));
      }
      return;
    }
    if (!metadata.isDirectory()) {
      throw new Error("registry smoke artifact is invalid");
    }
    for (const entry of (await readdir(path)).sort()) {
      await visit(join(path, entry));
    }
  }
  await visit(root);
  const aggregate = createRegistrySmokeAggregate(documents, version);
  await writePrivateJson(output, aggregate);
  return aggregate;
}

function parseArguments(arguments_) {
  const aggregate = arguments_.includes("--aggregate");
  const filtered = arguments_.filter((value) => value !== "--aggregate");
  const values = {};
  for (let index = 0; index < filtered.length; index += 2) {
    const flag = filtered[index];
    const value = filtered[index + 1];
    if (
      !["--directory", "--version", "--output"].includes(flag) ||
      typeof value !== "string" ||
      value.startsWith("--") ||
      Object.hasOwn(values, flag)
    ) {
      throw new Error("invalid registry smoke arguments");
    }
    values[flag] = value;
  }
  if (
    filtered.length % 2 !== 0 ||
    !Object.hasOwn(values, "--version") ||
    !Object.hasOwn(values, "--output") ||
    (aggregate !== Object.hasOwn(values, "--directory"))
  ) {
    throw new Error("invalid registry smoke arguments");
  }
  return { aggregate, values };
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : undefined;
if (invokedPath === fileURLToPath(import.meta.url)) {
  const parsed = parseArguments(process.argv.slice(2));
  if (parsed.aggregate) {
    await aggregateRegistrySmoke({
      directory: parsed.values["--directory"],
      version: parsed.values["--version"],
      output: parsed.values["--output"],
    });
  } else {
    await registrySmoke({
      version: parsed.values["--version"],
      output: parsed.values["--output"],
    });
  }
  process.stdout.write(`${parsed.values["--output"]}\n`);
}
