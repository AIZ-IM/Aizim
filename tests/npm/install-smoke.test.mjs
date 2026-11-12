import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmod, mkdir, mkdtemp, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import test from "node:test";

import {
  DOCTOR_CHECK_IDS,
  assertNoAgentDependencies,
  consumerEnvironment,
  createSmokeLayout,
  doctorFailureDocument,
  findExecutable,
  installArguments,
  validateProviderFreeCliEvidence,
  validatePrivateRoot,
  validateResearchTaskList,
} from "../../scripts/npm/install-smoke.mjs";

test("private smoke roots reject shared and non-private directories", async () => {
  await assert.rejects(
    validatePrivateRoot("/tmp", {
      lstat: async () => ({ isDirectory: () => true, mode: 0o40700, uid: 501 }),
      uid: 501,
    }),
    /private temporary root/,
  );
  await assert.rejects(
    validatePrivateRoot("/safe/aizim-npm-smoke-1", {
      lstat: async () => ({ isDirectory: () => true, mode: 0o40755, uid: 501 }),
      uid: 501,
    }),
    /private temporary root/,
  );
});

test("private smoke roots allow an owned mode-700 child of a shared temp root", async () => {
  const root = "/tmp/aizim-npm-smoke-1";

  assert.equal(
    await validatePrivateRoot(root, {
      lstat: async () => ({ isDirectory: () => true, mode: 0o40700, uid: 501 }),
      uid: 501,
    }),
    root,
  );
});

test("the smoke layout isolates every consumer-owned surface", () => {
  const layout = createSmokeLayout("/safe/aizim-npm-smoke-1");
  const values = [
    layout.local,
    layout.global,
    layout.project,
    layout.home,
    layout.cache,
    layout.npmPrefix,
    layout.metaOnly,
    layout.corrupt,
    layout.upgrade,
    layout.upgradeProject,
    layout.upgradePackages,
    layout.upgradeCache,
    layout.temporary,
  ];

  assert.equal(new Set(values).size, values.length);
  assert.ok(values.every((value) => value.startsWith(`${layout.root}/`)));
  assert.equal(
    layout.localBinary,
    `${layout.local}/node_modules/.bin/aizim`,
  );
  assert.equal(layout.globalBinary, `${layout.npmPrefix}/bin/aizim`);
});

test("installs exactly the platform and meta tarballs without scripts", () => {
  assert.deepEqual(
    installArguments("/dist/platform.tgz", "/dist/meta.tgz"),
    [
      "install",
      "--ignore-scripts",
      "--no-audit",
      "--no-fund",
      "/dist/platform.tgz",
      "/dist/meta.tgz",
    ],
  );
});

test("consumer environment is minimal and removes injection variables", () => {
  const layout = createSmokeLayout("/safe/aizim-npm-smoke-1");
  const environment = consumerEnvironment(
    layout,
    {
      HTTPS_PROXY: "https://proxy.invalid",
      AIZIM_DISTRIBUTION_MODE: "source",
      UV_INDEX_URL: "https://secret.invalid",
      PIP_INDEX_URL: "https://secret.invalid",
      PYTHONPATH: "/host/python",
      PATH: "/host/path",
      HOME: "/real/home",
    },
    ["/node/bin", "/usr/bin", "/bin"],
  );

  assert.deepEqual(environment, {
    AIZIM_CACHE_DIR: layout.cache,
    HOME: layout.home,
    HTTPS_PROXY: "https://proxy.invalid",
    LANG: "C.UTF-8",
    LC_ALL: "C.UTF-8",
    npm_config_prefix: layout.npmPrefix,
    PATH: "/node/bin:/usr/bin:/bin",
  });
});

test("npm and npx run with isolated consumers across Node installation layouts", async () => {
  const root = await mkdtemp(join(tmpdir(), "aizim-npm-tools-"));
  try {
    for (const separate of [false, true]) {
      const layoutRoot = join(root, separate ? "separate" : "bundled");
      const nodeBin = join(layoutRoot, "node", "bin");
      const npmBin = separate ? join(layoutRoot, "npm", "bin") : nodeBin;
      const tools = join(layoutRoot, "tools");
      await mkdir(nodeBin, { recursive: true });
      await mkdir(npmBin, { recursive: true });
      await mkdir(tools);
      await symlink(process.execPath, join(nodeBin, "node"));
      for (const name of ["npm", "npx"]) {
        const cli = join(npmBin, `${name}-cli.js`);
        await writeFile(cli, `#!/usr/bin/env node\nconsole.log(JSON.stringify({args:process.argv.slice(2),path:process.env.PATH,injection:process.env.NODE_OPTIONS??null}));\n`);
        await chmod(cli, 0o700);
        await symlink(cli, join(tools, name));
        const entrypoint = await findExecutable(name, [nodeBin, tools].join(delimiter));
        assert.equal(entrypoint, await realpath(cli));
        const layout = createSmokeLayout(join(layoutRoot, "consumer"));
        const environment = consumerEnvironment(layout, { NODE_OPTIONS: "--invalid-injection", PATH: tools }, [nodeBin]);
        const result = spawnSync(process.execPath, [entrypoint, "--version"], {
          env: environment,
          encoding: "utf8",
        });
        assert.equal(result.status, 0, result.stderr);
        assert.deepEqual(JSON.parse(result.stdout), {
          args: ["--version"], path: nodeBin, injection: null,
        });
      }
      await assert.rejects(findExecutable("npm", nodeBin === npmBin ? "" : nodeBin), /required npm executable is unavailable/);
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

const doctorCheckIds = [
  "python",
  "uv",
  "lean",
  "lake",
  "lean_project",
  "disk_floor",
  "runtime_mode",
  "omp",
  "magpie",
  "omp_rpc",
  "security_probe",
  "lean_lsp_mcp",
  "leanclient",
  "state_service",
];
const missingRuntimeChecks = new Set([
  "omp",
  "magpie",
  "omp_rpc",
  "security_probe",
]);

function missingRuntimeDoctor() {
  return {
    code: 3,
    signal: null,
    stdout: JSON.stringify({
      ready: false,
      checks: doctorCheckIds.map((id) => ({
        id,
        status: missingRuntimeChecks.has(id) ? "FAIL" : "PASS",
      })),
    }),
  };
}

test("runtime-free CLI evidence requires version, help, and stable doctor failure", () => {
  const doctor = missingRuntimeDoctor();
  assert.deepEqual(DOCTOR_CHECK_IDS, doctorCheckIds);
  assert.deepEqual(
    doctorFailureDocument(doctor, "runtime-free doctor"),
    JSON.parse(doctor.stdout),
  );
  assert.doesNotThrow(() =>
    validateProviderFreeCliEvidence({
      version: { code: 0, signal: null, stdout: "aizim 0.1.0\n" },
      help: { code: 0, signal: null, stdout: "usage: aizim [-h] {init,doctor,status,research}\n" },
      doctor,
    }),
  );
});

test("missing-runtime doctor accepts failed optional prerequisites", () => {
  const doctor = missingRuntimeDoctor();
  const document = JSON.parse(doctor.stdout);
  for (const check of document.checks) {
    check.status = "FAIL";
  }
  doctor.stdout = JSON.stringify(document);
  assert.doesNotThrow(() => doctorFailureDocument(doctor, "runtime-free doctor"));
});

test("missing-runtime doctor rejects skipped checks and runtime readiness claims", () => {
  for (const id of doctorCheckIds) {
    const doctor = missingRuntimeDoctor();
    const document = JSON.parse(doctor.stdout);
    document.checks.find((check) => check.id === id).status = "SKIP";
    doctor.stdout = JSON.stringify(document);
    assert.throws(() => doctorFailureDocument(doctor, "runtime-free doctor"));
  }
  for (const id of missingRuntimeChecks) {
    const doctor = missingRuntimeDoctor();
    const document = JSON.parse(doctor.stdout);
    document.checks.find((check) => check.id === id).status = "PASS";
    doctor.stdout = JSON.stringify(document);
    assert.throws(() => doctorFailureDocument(doctor, "runtime-free doctor"));
  }
});

test("missing-runtime doctor rejects incomplete checks and successful exits", () => {
  const doctor = missingRuntimeDoctor();
  const document = JSON.parse(doctor.stdout);
  document.checks.pop();
  assert.throws(() => doctorFailureDocument(
    { ...doctor, stdout: JSON.stringify(document) },
    "runtime-free doctor",
  ));
  assert.throws(() => doctorFailureDocument(
    { ...doctor, code: 0 },
    "runtime-free doctor",
  ));
  assert.throws(() => doctorFailureDocument(
    { ...doctor, signal: "SIGTERM" },
    "runtime-free doctor",
  ));
  assert.throws(() => doctorFailureDocument(
    { ...doctor, stdout: JSON.stringify({ ...JSON.parse(doctor.stdout), ready: true }) },
    "runtime-free doctor",
  ));
});

test("research smoke accepts an empty project and a persisted pending task", () => {
  const empty = { code: 0, signal: null, stdout: "[]" };
  assert.deepEqual(validateResearchTaskList(empty), []);
  const tasks = [{
    task_id: "addition",
    statement: "(n : Nat) : n + 0 = n",
    status: "pending",
    rounds: 0,
    verified_declarations: [],
  }];
  assert.deepEqual(
    validateResearchTaskList(
      { ...empty, stdout: JSON.stringify(tasks) },
      "research task roundtrip",
      true,
    ),
    tasks,
  );
});

test("research smoke rejects command failure, changed targets, and executed tasks", () => {
  for (const result of [
    { code: 1, signal: null, stdout: "[]" },
    { code: 0, signal: "SIGTERM", stdout: "[]" },
    { code: 0, signal: null, stdout: "not JSON" },
    { code: 0, signal: null, stdout: "{}" },
  ]) {
    assert.throws(() => validateResearchTaskList(result));
  }
  const task = {
    task_id: "addition",
    statement: "(n : Nat) : n + 0 = n",
    status: "pending",
    rounds: 0,
    verified_declarations: [],
  };
  for (const change of [
    { task_id: "another-target" },
    { statement: "False" },
    { status: "verified" },
    { rounds: 1 },
    { verified_declarations: ["addition_identity"] },
  ]) {
    assert.throws(() => validateResearchTaskList(
      { code: 0, signal: null, stdout: JSON.stringify([{ ...task, ...change }]) },
      "research task roundtrip",
      true,
    ));
  }
});

test("package metadata has no external agent dependency in any dependency class", () => {
  assert.doesNotThrow(() =>
    assertNoAgentDependencies({
      dependencies: { "ordinary-package": "1.0.0" },
      optionalDependencies: { "@aiz.im/aizim-linux-x64": "0.1.0" },
    }),
  );
  for (const dependencyClass of [
    "dependencies", "devDependencies", "optionalDependencies", "peerDependencies",
  ]) {
    for (const name of [
      "@openai/codex", "@anthropic-ai/claude-code",
      "@oh-my-pi/pi-coding-agent", "@kitten-science/magpie",
    ]) {
      assert.throws(
        () => assertNoAgentDependencies({ [dependencyClass]: { [name]: "1.0.0" } }),
        /agent dependency/u,
      );
    }
  }
});

