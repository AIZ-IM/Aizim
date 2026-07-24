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
  writeFile,
} from "node:fs/promises";
import { constants } from "node:fs";
import { tmpdir } from "node:os";
import {
  basename,
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
} from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

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
    npmPrefix,
    metaOnly: child("meta-only-consumer"),
    corrupt: child("corrupt-consumer"),
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
    PATH: [...new Set(pathEntries)].join(":"),
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

export function doctorDocument(result, label) {
  let document;
  try {
    document = JSON.parse(result.stdout);
  } catch {
    throw new Error(`${label} returned invalid JSON`);
  }
  const failures = document.checks
    ?.filter?.((check) => check.status === "FAIL")
    .map((check) => check.id);
  if (
    result.code !== 0 ||
    result.signal !== null ||
    document.ready !== true
  ) {
    throw new Error(
      `${label} failed checks (${failures?.join(",") || "unknown"})`,
    );
  }
  return document;
}

function requireCodexDoctor(document, label) {
  const codex = document.checks?.find?.((check) => check.id === "codex");
  if (
    codex?.status !== "PASS" ||
    !codex.detail.includes("0.145.0")
  ) {
    throw new Error(`${label} did not resolve Codex 0.145.0`);
  }
}

function requireClaudeDoctor(document, label) {
  const claude = document.checks?.find?.((check) => check.id === "claude");
  if (
    claude?.status !== "PASS" ||
    !claude.detail.includes("2.1.218 (Claude Code)")
  ) {
    throw new Error(`${label} did not resolve Claude Code 2.1.218`);
  }
}

async function installedClaudeExecutable(metaRoot) {
  const [{ resolveClaudeExecutable }, { detectTarget }] = await Promise.all([
    import(pathToFileURL(join(metaRoot, "lib", "claude.mjs"))),
    import(pathToFileURL(join(metaRoot, "lib", "platform.mjs"))),
  ]);
  return resolveClaudeExecutable(detectTarget());
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

async function toolPath() {
  return [
    dirname(process.execPath),
    dirname(await findExecutable("lake")),
    "/usr/bin",
    "/bin",
  ];
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
      await toolPath(),
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
    const localClaude = await requireSuccess(
      await installedClaudeExecutable(
        join(layout.local, "node_modules", "@aiz.im", "aizim"),
      ),
      ["--version"],
      { cwd: layout.local, env: environment },
      "local Claude version",
    );
    if (localClaude.stdout.trim() !== "2.1.218 (Claude Code)") {
      throw new Error("local Claude version mismatch");
    }

    const version = await requireSuccess(
      layout.localBinary,
      ["--version"],
      { cwd: layout.local, env: environment },
      "first local version",
    );
    if (version.stdout.trim() !== "aizim 0.1.0") {
      throw new Error("local version mismatch");
    }
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
    const doctor = await requireSuccess(
      layout.localBinary,
      ["doctor", "--project", layout.project],
      { cwd: layout.local, env: environment },
      "npm doctor",
    );
    if (!doctor.stdout.split(/\r?\n/u).includes("READY")) {
      throw new Error("npm doctor did not report READY");
    }
    const localDoctorResult = await execute(
      layout.localBinary,
      ["doctor", "--project", layout.project, "--json"],
      { cwd: layout.local, env: environment },
    );
    const localDoctor = doctorDocument(localDoctorResult, "npm doctor");
    requireCodexDoctor(localDoctor, "npm doctor");
    requireClaudeDoctor(localDoctor, "npm doctor");
    const gate = await requireSuccess(
      layout.localBinary,
      [
        "security-probe",
        "--project",
        layout.project,
        "--backend",
        "codex",
        "--no-model",
      ],
      { cwd: layout.local, env: environment },
      "npm security gate",
    );
    if (!gate.stdout.startsWith("SECURITY GATE PASS\n")) {
      throw new Error("npm security gate mismatch");
    }
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
    const globalClaude = await requireSuccess(
      await installedClaudeExecutable(
        join(
          layout.npmPrefix,
          "lib",
          "node_modules",
          "@aiz.im",
          "aizim",
        ),
      ),
      ["--version"],
      { cwd: layout.global, env: environment },
      "global Claude version",
    );
    if (globalClaude.stdout.trim() !== "2.1.218 (Claude Code)") {
      throw new Error("global Claude version mismatch");
    }
    const globalDoctorResult = await execute(
      layout.globalBinary,
      ["doctor", "--project", layout.project, "--json"],
      { cwd: layout.global, env: environment },
    );
    const globalDoctor = doctorDocument(
      globalDoctorResult,
      "global npm doctor",
    );
    requireCodexDoctor(globalDoctor, "global npm doctor");
    requireClaudeDoctor(globalDoctor, "global npm doctor");

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
        local_install: true,
        global_install: true,
        npx_no_install: true,
        python_314_bootstrap: true,
        cache_reused: true,
        uninstall_preserved_cache: true,
        local_codex_01450: true,
        global_codex_01450: true,
        local_claude_21218: true,
        global_claude_21218: true,
        ready: true,
        security_gate: true,
        aizim_run: true,
        controller_loop: true,
        missing_platform_exit_78: true,
        integrity_failure_exit_74: true,
      },
    });
    process.stdout.write(
      "aizim 0.1.0\nREADY\nSECURITY GATE PASS\nAIZIM RUN PASS\n" +
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
