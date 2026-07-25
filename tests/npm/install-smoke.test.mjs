import assert from "node:assert/strict";
import test from "node:test";

import {
  DOCTOR_CHECK_IDS,
  assertNoAgentDependencies,
  consumerEnvironment,
  createSmokeLayout,
  doctorFailureDocument,
  installArguments,
  validateProviderFreeCliEvidence,
  validatePrivateRoot,
} from "../../scripts/npm/install-smoke.mjs";
import * as installSmokeModule from "../../scripts/npm/install-smoke.mjs";

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
    layout.runProject,
    layout.home,
    layout.cache,
    layout.npmPrefix,
    layout.metaOnly,
    layout.corrupt,
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
    ["/node/bin", "/lean/bin", "/usr/bin", "/bin"],
  );

  assert.deepEqual(environment, {
    AIZIM_CACHE_DIR: layout.cache,
    ELAN_HOME: "/real/home/.elan",
    HOME: layout.home,
    HTTPS_PROXY: "https://proxy.invalid",
    LANG: "C.UTF-8",
    LC_ALL: "C.UTF-8",
    npm_config_prefix: layout.npmPrefix,
    PATH: "/node/bin:/lean/bin:/usr/bin:/bin",
  });
});

test("provider-free CLI evidence requires version, help, and stable doctor failure", () => {
  const checks = DOCTOR_CHECK_IDS.map((id) => ({
    id,
    status: {
      controller_configuration: "FAIL",
      controller_provider: "SKIP",
      controller_executable: "SKIP",
      controller_auth: "SKIP",
      worker_codex: "FAIL",
      sandbox_exec: "SKIP",
    }[id] ?? "PASS",
  }));
  const doctor = {
    code: 3,
    signal: null,
    stdout: JSON.stringify({
      ready: false,
      checks,
    }),
  };

  assert.deepEqual(
    doctorFailureDocument(doctor, "provider-free doctor").checks,
    checks,
  );
  assert.doesNotThrow(() =>
    validateProviderFreeCliEvidence({
      version: { code: 0, signal: null, stdout: "aizim 0.1.0\n" },
      help: { code: 0, signal: null, stdout: "usage: aizim [-h]\n" },
      doctor,
    }),
  );
});

test("provider-free metadata has no agent dependency in any dependency class", () => {
  assert.doesNotThrow(() =>
    assertNoAgentDependencies({
      dependencies: { "ordinary-package": "1.0.0" },
      optionalDependencies: { "@aiz.im/aizim-linux-x64": "0.1.0" },
    }),
  );
  assert.throws(
    () =>
      assertNoAgentDependencies({
        dependencies: { "@openai/codex": "0.145.0" },
      }),
    /agent dependency/u,
  );
  assert.throws(
    () =>
      assertNoAgentDependencies({
        peerDependencies: { "@anthropic-ai/claude-code": "2.1.218" },
      }),
    /agent dependency/u,
  );
});

test("controller smoke uses the provisioned wheel runtime and exact output", () => {
  assert.equal(
    typeof installSmokeModule.provisionedRuntimePython,
    "function",
  );
  assert.equal(
    installSmokeModule.provisionedRuntimePython(
      "/cache/runtime/v1/0.1.0/target/hash/py3.14.6/READY.json",
    ),
    "/cache/runtime/v1/0.1.0/target/hash/py3.14.6/venv/bin/python",
  );
  assert.equal(
    typeof installSmokeModule.requireControllerSmokeOutput,
    "function",
  );
  const output =
    "CONTROLLER SMOKE PASS\n" +
    "assignment_executions=1\n" +
    "restart_duplicates=0\n";
  assert.equal(
    installSmokeModule.requireControllerSmokeOutput({ stdout: output }),
    output,
  );
  assert.throws(
    () => installSmokeModule.requireControllerSmokeOutput({
      stdout: "CONTROLLER SMOKE PASS\nassignment_executions=2\nrestart_duplicates=0\n",
    }),
    /controller smoke output mismatch/u,
  );
});
