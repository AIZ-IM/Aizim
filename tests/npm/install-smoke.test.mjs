import assert from "node:assert/strict";
import test from "node:test";

import {
  consumerEnvironment,
  createSmokeLayout,
  doctorDocument,
  installArguments,
  validatePrivateRoot,
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

test("doctor failures name the exact failed checks", () => {
  const result = {
    code: 3,
    signal: null,
    stdout: JSON.stringify({
      ready: false,
      checks: [
        { id: "disk_floor", status: "FAIL" },
        { id: "codex", status: "PASS" },
      ],
    }),
  };

  assert.throws(
    () => doctorDocument(result, "global npm doctor"),
    /global npm doctor failed checks \(disk_floor\)/u,
  );
});
