import { spawn } from "node:child_process";
import {
  access,
  chmod,
  cp,
  lstat,
  mkdir,
  mkdtemp,
  realpath,
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
  relative,
  resolve,
} from "node:path";
import { fileURLToPath } from "node:url";

import { capture } from "./lib/command.mjs";
import { sha256File } from "./lib/hash.mjs";
import {
  npmBuildRoot,
  npmDistRoot,
  repositoryRoot,
} from "./lib/paths.mjs";
import { pack } from "./pack.mjs";

const evidencePath = join(npmBuildRoot, "install-smoke-evidence.json");
const preservedEnvironment = new Set([
  "HTTP_PROXY",
  "HTTPS_PROXY",
  "NO_PROXY",
  "SSL_CERT_DIR",
  "SSL_CERT_FILE",
]);
const sharedTemporaryRoots = [
  "/tmp",
  "/private/tmp",
  "/var/tmp",
  "/private/var/tmp",
];
const agentPackages = Object.freeze([
  "@openai/codex",
  "@anthropic-ai/claude-code",
]);
const agentCommands = Object.freeze(["codex", "claude"]);
export const DOCTOR_CHECK_IDS = Object.freeze([
  "python",
  "uv",
  "lean",
  "lake",
  "lean_project",
  "disk_floor",
  "runtime_mode",
  "controller_configuration",
  "controller_provider",
  "controller_executable",
  "controller_auth",
  "worker_codex",
  "sandbox_exec",
  "lean_lsp_mcp",
  "leanclient",
  "state_service",
]);

export function createSmokeLayout(root) {
  const child = (name) => join(root, name);
  const local = child("local-consumer");
  const npmPrefix = child("npm-prefix");
  return Object.freeze({
    root,
    local,
    global: child("global-consumer"),
    project: child("project"),
    runProject: child("run-project"),
    home: child("home"),
    cache: child("cache"),
    tools: child("tools"),
    npmPrefix,
    metaOnly: child("meta-only-consumer"),
    corrupt: child("corrupt-consumer"),
    upgrade: child("upgrade-consumer"),
    upgradeProject: child("upgrade-project"),
    upgradePackages: child("upgrade-packages"),
    upgradeCache: child("upgrade-cache"),
    temporary: child("tmp"),
    localBinary: join(local, "node_modules", ".bin", "aizim"),
    globalBinary: join(npmPrefix, "bin", "aizim"),
  });
}

export function installArguments(platformTarball, metaTarball) {
  return [
    "install",
    "--ignore-scripts",
    "--no-audit",
    "--no-fund",
    platformTarball,
    metaTarball,
  ];
}

export function consumerEnvironment(layout, source, pathEntries) {
  const environment = Object.fromEntries(
    Object.entries(source).filter(([name]) => preservedEnvironment.has(name)),
  );
  const elanHome =
    source.ELAN_HOME ?? (source.HOME ? join(source.HOME, ".elan") : undefined);
  return {
    ...environment,
    AIZIM_CACHE_DIR: layout.cache,
    ...(elanHome ? { ELAN_HOME: elanHome } : {}),
    HOME: layout.home,
    LANG: "C.UTF-8",
    LC_ALL: "C.UTF-8",
    npm_config_prefix: layout.npmPrefix,
    PATH: [...new Set(pathEntries)].join(delimiter),
  };
}

export async function validatePrivateRoot(
  root,
  dependencies = { lstat, realpath, uid: process.getuid() },
) {
  const absolute = resolve(root);
  const canonical = dependencies.realpath
    ? await dependencies.realpath(absolute)
    : absolute;
  const metadata = await dependencies.lstat(canonical);
  const shared = sharedTemporaryRoots.includes(canonical);
  if (
    !isAbsolute(root) ||
    shared ||
    !basename(canonical).startsWith("aizim-npm-smoke-") ||
    !metadata.isDirectory() ||
    metadata.uid !== dependencies.uid ||
    (metadata.mode & 0o777) !== 0o700
  ) {
    throw new Error("invalid private temporary root");
  }
  return canonical;
}

function npmArguments(...arguments_) {
  return [process.execPath, [join(dirname(process.execPath), "npm"), ...arguments_]];
}

function npxArguments(...arguments_) {
  return [process.execPath, [join(dirname(process.execPath), "npx"), ...arguments_]];
}

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
    child.once("error", () => reject(new Error("smoke command failed to start")));
    child.once("exit", (code, signal) => {
      resolvePromise({
        code: Number.isInteger(code) ? code : 70,
        signal,
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
      });
    });
  });
}

async function requireSuccess(program, arguments_, options, label) {
  const result = await execute(program, arguments_, options);
  if (result.code !== 0 || result.signal !== null) {
    const stableCode =
      result.stderr.match(/\b[A-Z][A-Z0-9_]{2,}\b/u)?.[0] ?? "UNKNOWN";
    throw new Error(`${label} failed (${result.code}:${stableCode})`);
  }
  return result;
}

function parsedDoctor(result, label) {
  let document;
  try {
    document = JSON.parse(result.stdout);
  } catch {
    throw new Error(`${label} returned invalid JSON`);
  }
  const ids = document.checks?.map?.((check) => check.id);
  if (
    !Array.isArray(ids) ||
    ids.length !== DOCTOR_CHECK_IDS.length ||
    ids.some((id, index) => id !== DOCTOR_CHECK_IDS[index])
  ) {
    throw new Error(`${label} check set mismatch`);
  }
  return document;
}

export function doctorFailureDocument(result, label) {
  const document = parsedDoctor(result, label);
  const statuses = Object.fromEntries(
    document.checks.map((check) => [check.id, check.status]),
  );
  const expectedStatuses = {
    controller_configuration: "FAIL",
    controller_provider: "SKIP",
    controller_executable: "SKIP",
    controller_auth: "SKIP",
    worker_codex: "FAIL",
    sandbox_exec: "SKIP",
  };
  if (
    result.code !== 3 ||
    result.signal !== null ||
    document.ready !== false ||
    DOCTOR_CHECK_IDS.some(
      (id) => statuses[id] !== (expectedStatuses[id] ?? "PASS"),
    )
  ) {
    throw new Error(`${label} did not fail closed by role`);
  }
  return document;
}

export function validateProviderFreeCliEvidence(
  { version, help, doctor },
  label = "provider-free CLI",
  expectedVersion = "0.1.0",
) {
  if (
    version.code !== 0 ||
    version.signal !== null ||
    version.stdout.trim() !== `aizim ${expectedVersion}`
  ) {
    throw new Error(`${label} version mismatch`);
  }
  if (
    help.code !== 0 ||
    help.signal !== null ||
    !help.stdout.startsWith("usage: aizim ")
  ) {
    throw new Error(`${label} help mismatch`);
  }
  doctorFailureDocument(doctor, `${label} doctor`);
}

export function assertNoAgentDependencies(document, label = "package") {
  for (const dependencyClass of [
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
  ]) {
    const dependencies = document[dependencyClass] ?? {};
    if (agentPackages.some((name) => Object.hasOwn(dependencies, name))) {
      throw new Error(`${label} contains an agent dependency`);
    }
  }
}

async function writeConsumer(path) {
  await mkdir(path, { mode: 0o700, recursive: true });
  await writeFile(
    join(path, "package.json"),
    '{"name":"aizim-install-smoke","version":"0.0.0","private":true}\n',
    { mode: 0o600 },
  );
}

async function findExecutable(name) {
  for (const directory of (process.env.PATH ?? "").split(":")) {
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

async function toolPath(layout) {
  await mkdir(layout.tools, { mode: 0o700 });
  await symlink(process.execPath, join(layout.tools, "node"));
  for (const name of ["lake", "lean", "rg"]) {
    await symlink(await findExecutable(name), join(layout.tools, name));
  }
  const paths = [layout.tools, "/usr/bin", "/bin"];
  for (const name of agentCommands) {
    for (const directory of paths) {
      try {
        await access(join(directory, name), constants.X_OK);
      } catch {
        continue;
      }
      throw new Error(`provider-free PATH exposes ${name}`);
    }
  }
  return paths;
}

async function readyMarkers(root) {
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
    sha256: await sha256File(path),
    mtime_ns: metadata.mtimeNs.toString(),
  };
}

export function provisionedRuntimePython(readyMarker) {
  return join(dirname(readyMarker), "venv", "bin", "python");
}

export function requireControllerSmokeOutput(result) {
  const expected =
    "CONTROLLER SMOKE PASS\n" +
    "assignment_executions=1\n" +
    "restart_duplicates=0\n";
  if (result.stdout !== expected) {
    throw new Error("controller smoke output mismatch");
  }
  return result.stdout;
}

async function packageInputs() {
  await pack();
  const summary = JSON.parse(
    await readFile(join(npmDistRoot, "pack-summary.json"), "utf8"),
  );
  if (
    summary.schema_version !== 1 ||
    summary.git_sha !==
      (await capture("git", ["rev-parse", "HEAD"], { cwd: repositoryRoot })) ||
    !Array.isArray(summary.packages) ||
    summary.packages.length !== 2
  ) {
    throw new Error("pack summary mismatch");
  }
  const meta = summary.packages.find(({ name }) => name === "@aiz.im/aizim");
  const platform = summary.packages.find(({ name }) => name !== "@aiz.im/aizim");
  if (
    !meta ||
    !platform ||
    platform.name !== `@aiz.im/aizim-${summary.target}`
  ) {
    throw new Error("pack summary mismatch");
  }
  for (const record of [platform, meta]) {
    const path = join(npmDistRoot, record.filename);
    if (
      basename(record.filename) !== record.filename ||
      (await sha256File(path)) !== record.sha256
    ) {
      throw new Error("packed artifact mismatch");
    }
    record.path = path;
  }
  return { meta, platform, summary };
}

async function readPackageDocument(root) {
  return JSON.parse(await readFile(join(root, "package.json"), "utf8"));
}

async function pathExists(path) {
  try {
    await lstat(path);
    return true;
  } catch (error) {
    if (error?.code === "ENOENT") {
      return false;
    }
    throw error;
  }
}

async function packUpgradeFixture(npm, layout, name, environment) {
  const fixture = join(
    repositoryRoot,
    "tests",
    "fixtures",
    "npm-bundled-agent-upgrade",
    name,
  );
  const result = await requireSuccess(
    ...npm(
      "pack",
      "--ignore-scripts",
      "--json",
      "--pack-destination",
      layout.upgradePackages,
      fixture,
    ),
    { cwd: layout.upgrade, env: environment },
    `pack legacy ${name} fixture`,
  );
  const parsed = JSON.parse(result.stdout);
  const records = Array.isArray(parsed) ? parsed : Object.values(parsed);
  const filename = records?.[0]?.filename;
  if (
    records.length !== 1 ||
    typeof filename !== "string" ||
    basename(filename) !== filename
  ) {
    throw new Error(`legacy ${name} fixture pack mismatch`);
  }
  return join(layout.upgradePackages, filename);
}

function requireConfiguredCodexFailure(result) {
  const document = parsedDoctor(result, "configured Codex upgrade doctor");
  const statuses = Object.fromEntries(
    document.checks.map((check) => [check.id, check.status]),
  );
  if (
    result.code !== 3 ||
    result.signal !== null ||
    document.ready !== false ||
    statuses.controller_configuration !== "PASS" ||
    statuses.controller_provider !== "PASS" ||
    statuses.controller_executable !== "FAIL" ||
    statuses.controller_auth !== "SKIP" ||
    statuses.worker_codex !== "FAIL" ||
    statuses.sandbox_exec !== "SKIP"
  ) {
    throw new Error("configured Codex upgrade doctor did not fail closed");
  }
}

async function bundledAgentUpgrade({
  environment,
  initializer,
  layout,
  npm,
  packages,
}) {
  const [legacy, codex, claude] = await Promise.all([
    packUpgradeFixture(npm, layout, "aizim", environment),
    packUpgradeFixture(npm, layout, "codex", environment),
    packUpgradeFixture(npm, layout, "claude", environment),
  ]);
  await writeFile(
    join(layout.upgrade, "package.json"),
    `${JSON.stringify(
      {
        name: "aizim-bundled-agent-upgrade",
        version: "0.0.0",
        private: true,
        dependencies: { "@aiz.im/aizim": `file:${legacy}` },
        overrides: {
          "@openai/codex": `file:${codex}`,
          "@anthropic-ai/claude-code": `file:${claude}`,
        },
      },
      null,
      2,
    )}\n`,
    { mode: 0o600 },
  );
  await requireSuccess(
    ...npm(
      "install",
      "--ignore-scripts",
      "--offline",
      "--cache",
      layout.upgradeCache,
      "--no-audit",
      "--no-fund",
    ),
    { cwd: layout.upgrade, env: environment },
    "legacy bundled-agent install",
  );
  const legacyRoot = join(
    layout.upgrade,
    "node_modules",
    "@aiz.im",
    "aizim",
  );
  const legacyDocument = await readPackageDocument(legacyRoot);
  if (
    legacyDocument.dependencies?.["@openai/codex"] !== "0.145.0" ||
    legacyDocument.dependencies?.["@anthropic-ai/claude-code"] !== "2.1.218"
  ) {
    throw new Error("legacy bundled-agent dependency mismatch");
  }
  for (const name of agentCommands) {
    if (!(await pathExists(join(layout.upgrade, "node_modules", ".bin", name)))) {
      throw new Error(`legacy ${name} fixture is unavailable`);
    }
  }

  await copyFixture(layout.upgradeProject);
  await requireSuccess(
    initializer,
    ["init", layout.upgradeProject],
    { cwd: layout.local, env: environment },
    "upgrade project init",
  );
  const stateDatabase = join(
    layout.upgradeProject,
    ".aizim",
    "state.sqlite3",
  );
  const credentialSentinels = [
    join(layout.home, ".codex", "upgrade-credential-sentinel"),
    join(layout.home, ".claude", "upgrade-credential-sentinel"),
  ];
  for (const [index, sentinel] of credentialSentinels.entries()) {
    await mkdir(dirname(sentinel), { mode: 0o700, recursive: true });
    await writeFile(sentinel, `credential-${index}\n`, { mode: 0o600 });
  }
  const before = await Promise.all(
    [stateDatabase, ...credentialSentinels].map(sha256File),
  );

  await requireSuccess(
    ...npm(
      "install",
      "--ignore-scripts",
      "--offline",
      "--cache",
      layout.upgradeCache,
      "--no-audit",
      "--no-fund",
      packages.platform.path,
      packages.meta.path,
    ),
    { cwd: layout.upgrade, env: environment },
    "provider-free upgrade install",
  );
  const after = await Promise.all(
    [stateDatabase, ...credentialSentinels].map(sha256File),
  );
  if (JSON.stringify(before) !== JSON.stringify(after)) {
    throw new Error("provider-free upgrade changed state or credentials");
  }
  const upgradedRoot = join(
    layout.upgrade,
    "node_modules",
    "@aiz.im",
    "aizim",
  );
  assertNoAgentDependencies(
    await readPackageDocument(upgradedRoot),
    "upgraded package",
  );
  for (const name of [...agentCommands, ...agentPackages]) {
    const path = name.startsWith("@")
      ? join(layout.upgrade, "node_modules", ...name.split("/"))
      : join(layout.upgrade, "node_modules", ".bin", name);
    if (await pathExists(path)) {
      throw new Error(`upgrade retained legacy provider ${name}`);
    }
  }

  const binary = join(layout.upgrade, "node_modules", ".bin", "aizim");
  const version = await execute(binary, ["--version"], {
    cwd: layout.upgrade,
    env: environment,
  });
  const help = await execute(binary, ["--help"], {
    cwd: layout.upgrade,
    env: environment,
  });
  if (
    version.code !== 0 ||
    version.stdout.trim() !== "aizim 0.1.0" ||
    help.code !== 0 ||
    !help.stdout.startsWith("usage: aizim ")
  ) {
    throw new Error("upgraded provider-free CLI mismatch");
  }
  await requireSuccess(
    binary,
    [
      "controller",
      "configure",
      "--project",
      layout.upgradeProject,
      "--provider",
      "codex",
    ],
    { cwd: layout.upgrade, env: environment },
    "upgrade Codex controller configure",
  );
  requireConfiguredCodexFailure(
    await execute(
      binary,
      ["doctor", "--project", layout.upgradeProject, "--json"],
      { cwd: layout.upgrade, env: environment },
    ),
  );
}

async function copyFixture(destination) {
  await cp(
    join(repositoryRoot, "tests", "fixtures", "attack_probe_project"),
    destination,
    {
      recursive: true,
      filter: (source) => basename(source) !== ".aizim",
    },
  );
}

async function copyRunFixture(destination) {
  await cp(join(repositoryRoot, "examples", "smoke_lean"), destination, {
    recursive: true,
    filter: (source) => basename(source) !== ".aizim",
  });
}

async function writeEvidence(document) {
  await mkdir(npmBuildRoot, { mode: 0o700, recursive: true });
  const temporary = `${evidencePath}.tmp`;
  await writeFile(temporary, `${JSON.stringify(document, null, 2)}\n`, {
    mode: 0o600,
  });
  await rename(temporary, evidencePath);
}

export async function installSmoke() {
  await rm(evidencePath, { force: true });
  const generated = await mkdtemp(join(tmpdir(), "aizim-npm-smoke-"));
  await chmod(generated, 0o700);
  const root = await validatePrivateRoot(generated);
  const layout = createSmokeLayout(root);
  try {
    for (const path of [
      layout.global,
      layout.home,
      layout.cache,
      layout.npmPrefix,
      layout.project,
      layout.runProject,
      layout.upgrade,
      layout.upgradeProject,
      layout.upgradePackages,
      layout.upgradeCache,
      layout.temporary,
    ]) {
      await mkdir(path, { mode: 0o700, recursive: true });
    }
    for (const path of [layout.local, layout.metaOnly, layout.corrupt]) {
      await writeConsumer(path);
    }
    const packages = await packageInputs();
    const environment = consumerEnvironment(
      layout,
      process.env,
      await toolPath(layout),
    );
    environment.TMPDIR = layout.temporary;
    const npm = (...arguments_) => npmArguments(...arguments_);
    const npx = (...arguments_) => npxArguments(...arguments_);
    let command = npm(
      ...installArguments(packages.platform.path, packages.meta.path),
    );
    await requireSuccess(...command, {
      cwd: layout.local,
      env: environment,
    }, "local install");
    assertNoAgentDependencies(
      await readPackageDocument(
        join(layout.local, "node_modules", "@aiz.im", "aizim"),
      ),
      "local package",
    );

    const version = await requireSuccess(
      layout.localBinary,
      ["--version"],
      { cwd: layout.local, env: environment },
      "first local version",
    );
    if (version.stdout.trim() !== "aizim 0.1.0") {
      throw new Error("local version mismatch");
    }
    const help = await requireSuccess(
      layout.localBinary,
      ["--help"],
      { cwd: layout.local, env: environment },
      "local help",
    );
    const markers = await readyMarkers(layout.cache);
    if (markers.length !== 1) {
      throw new Error("runtime ready marker mismatch");
    }
    const firstReady = await readyRecord(markers[0]);
    const repeatedVersion = await requireSuccess(
      layout.localBinary,
      ["--version"],
      { cwd: layout.local, env: environment },
      "second local version",
    );
    const secondReady = await readyRecord(markers[0]);
    if (
      repeatedVersion.stdout.trim() !== version.stdout.trim() ||
      JSON.stringify(firstReady) !== JSON.stringify(secondReady)
    ) {
      throw new Error("runtime cache was not reused");
    }

    command = npx("--no-install", "aizim", "--version");
    const npxVersion = await requireSuccess(...command, {
      cwd: layout.local,
      env: environment,
    }, "npx no-install");
    if (npxVersion.stdout.trim() !== "aizim 0.1.0") {
      throw new Error("npx version mismatch");
    }

    await copyFixture(layout.project);
    await requireSuccess(
      layout.localBinary,
      ["init", layout.project],
      { cwd: layout.local, env: environment },
      "npm init",
    );
    const localDoctorResult = await execute(
      layout.localBinary,
      ["doctor", "--project", layout.project, "--json"],
      { cwd: layout.local, env: environment },
    );
    validateProviderFreeCliEvidence(
      { version, help, doctor: localDoctorResult },
      "local provider-free CLI",
    );
    await copyRunFixture(layout.runProject);
    await requireSuccess(
      await findExecutable("lake"),
      ["build"],
      { cwd: layout.runProject, env: environment },
      "npm run project build",
    );
    await requireSuccess(
      layout.localBinary,
      ["init", layout.runProject],
      { cwd: layout.local, env: environment },
      "npm run project init",
    );
    const runResult = await requireSuccess(
      layout.localBinary,
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
      "npm deterministic run",
    );
    if (!runResult.stdout.includes("AIZIM RUN PASS")) {
      throw new Error("npm run result mismatch");
    }
    const controllerSmoke = await requireSuccess(
      provisionedRuntimePython(markers[0]),
      [join(repositoryRoot, "scripts", "qa", "controller_smoke.py")],
      { cwd: repositoryRoot, env: environment },
      "npm controller loop",
    );
    requireControllerSmokeOutput(controllerSmoke);
    await bundledAgentUpgrade({
      environment,
      initializer: layout.localBinary,
      layout,
      npm,
      packages,
    });

    const sentinel = join(layout.cache, "unrelated-sentinel");
    await writeFile(sentinel, "preserve\n", { mode: 0o600 });
    const beforeUninstall = {
      ready: await readyRecord(markers[0]),
      sentinel: await sha256File(sentinel),
    };
    command = npm(
      "uninstall",
      "--ignore-scripts",
      "--no-audit",
      "--no-fund",
      packages.platform.name,
      packages.meta.name,
    );
    await requireSuccess(...command, {
      cwd: layout.local,
      env: environment,
    }, "local uninstall");
    const afterUninstall = {
      ready: await readyRecord(markers[0]),
      sentinel: await sha256File(sentinel),
    };
    if (JSON.stringify(beforeUninstall) !== JSON.stringify(afterUninstall)) {
      throw new Error("uninstall changed the runtime cache");
    }

    command = npm(
      "install",
      "--global",
      "--ignore-scripts",
      "--no-audit",
      "--no-fund",
      "--prefix",
      layout.npmPrefix,
      packages.platform.path,
      packages.meta.path,
    );
    await requireSuccess(...command, {
      cwd: layout.global,
      env: environment,
    }, "global install");
    const globalVersion = await requireSuccess(
      layout.globalBinary,
      ["--version"],
      { cwd: layout.global, env: environment },
      "global version",
    );
    if (globalVersion.stdout.trim() !== "aizim 0.1.0") {
      throw new Error("global version mismatch");
    }
    const globalHelp = await requireSuccess(
      layout.globalBinary,
      ["--help"],
      { cwd: layout.global, env: environment },
      "global help",
    );
    assertNoAgentDependencies(
      await readPackageDocument(
        join(
          layout.npmPrefix,
          "lib",
          "node_modules",
          "@aiz.im",
          "aizim",
        ),
      ),
      "global package",
    );
    const globalDoctorResult = await execute(
      layout.globalBinary,
      ["doctor", "--project", layout.project, "--json"],
      { cwd: layout.global, env: environment },
    );
    validateProviderFreeCliEvidence(
      {
        version: globalVersion,
        help: globalHelp,
        doctor: globalDoctorResult,
      },
      "global provider-free CLI",
    );

    command = npm(
      "install",
      "--ignore-scripts",
      "--no-audit",
      "--no-fund",
      "--omit=optional",
      packages.meta.path,
    );
    await requireSuccess(...command, {
      cwd: layout.metaOnly,
      env: environment,
    }, "meta-only install");
    const missing = await execute(
      join(layout.metaOnly, "node_modules", ".bin", "aizim"),
      ["--version"],
      { cwd: layout.metaOnly, env: environment },
    );
    if (missing.code !== 78 || !missing.stderr.includes("PLATFORM_PACKAGE_MISSING")) {
      throw new Error("missing platform did not fail closed");
    }

    command = npm(
      ...installArguments(packages.platform.path, packages.meta.path),
    );
    await requireSuccess(...command, {
      cwd: layout.corrupt,
      env: environment,
    }, "corrupt fixture install");
    const manifest = join(
      layout.corrupt,
      "node_modules",
      "@aiz.im",
      "aizim",
      "manifest",
      "distribution.json",
    );
    const document = JSON.parse(await readFile(manifest, "utf8"));
    document.wheel.sha256 = "0".repeat(64);
    await writeFile(manifest, `${JSON.stringify(document)}\n`, { mode: 0o600 });
    const corrupt = await execute(
      join(layout.corrupt, "node_modules", ".bin", "aizim"),
      ["--version"],
      { cwd: layout.corrupt, env: environment },
    );
    if (corrupt.code !== 74) {
      throw new Error("corrupt manifest did not fail closed");
    }

    await writeEvidence({
      schema_version: 1,
      commit_sha: packages.summary.git_sha,
      target: packages.summary.target,
      node_version: process.version,
      packages: [packages.platform, packages.meta].map(
        ({ name, filename, size, sha256, integrity }) => ({
          name,
          filename,
          size,
          sha256,
          integrity,
        }),
      ),
      ready: {
        first: firstReady,
        second: secondReady,
        reused: true,
      },
      checks: {
        provider_free_local_install: true,
        provider_free_global_install: true,
        provider_free_version: true,
        provider_free_help: true,
        provider_free_doctor_fails_closed: true,
        no_agent_dependencies: true,
        python_314_bootstrap: true,
        cache_reused: true,
        uninstall_preserved_cache: true,
        aizim_run: true,
        controller_loop: true,
        bundled_agent_upgrade_preserved_state_credentials: true,
        missing_platform_exit_78: true,
        integrity_failure_exit_74: true,
      },
    });
    process.stdout.write(
      "aizim 0.1.0\nEXPECTED DOCTOR FAILURE\nAIZIM RUN PASS\n" +
      controllerSmoke.stdout,
    );
  } finally {
    await rm(root, { force: true, recursive: true });
  }
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : undefined;
if (invokedPath === fileURLToPath(import.meta.url)) {
  await installSmoke();
}
