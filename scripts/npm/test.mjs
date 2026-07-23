import {
  mkdir,
  readFile,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import { dirname, join } from "node:path";

import { run } from "./lib/command.mjs";
import {
  npmBuildRoot,
  repositoryRoot,
} from "./lib/paths.mjs";
import { verifyFreshBuild } from "./pack.mjs";
import { validateInstallSmokeEvidence } from "./write-ci-evidence.mjs";

const installEvidencePath = join(
  npmBuildRoot,
  "install-smoke-evidence.json",
);
const testSummaryPath = join(npmBuildRoot, "test-summary.json");

async function testDistribution() {
  await rm(testSummaryPath, { force: true });
  const build = await verifyFreshBuild();
  for (const [program, arguments_] of [
    ["node", ["--test", "tests/npm/*.test.mjs"]],
    ["cargo", ["fmt", "--all", "--check"]],
    [
      "cargo",
      [
        "clippy",
        "--workspace",
        "--all-targets",
        "--locked",
        "--",
        "-D",
        "warnings",
      ],
    ],
    ["cargo", ["test", "--workspace", "--locked"]],
    ["node", ["scripts/npm/test-python.mjs"]],
    ["node", ["scripts/npm/install-smoke.mjs"]],
    ["node", ["scripts/npm/check.mjs"]],
  ]) {
    await run(program, arguments_, { cwd: repositoryRoot });
  }
  const install = validateInstallSmokeEvidence(
    JSON.parse(await readFile(installEvidencePath, "utf8")),
  );
  if (
    install.commit_sha !== build.git_sha ||
    install.target !== build.target
  ) {
    throw new Error("install evidence does not match the tested build");
  }
  const summary = {
    schema_version: 1,
    commit_sha: build.git_sha,
    target: build.target,
    checks: {
      node: true,
      rust: true,
      python: true,
      install_smoke: true,
      package_check: true,
    },
  };
  await mkdir(dirname(testSummaryPath), { mode: 0o700, recursive: true });
  const temporary = `${testSummaryPath}.tmp-${process.pid}`;
  await writeFile(temporary, `${JSON.stringify(summary, null, 2)}\n`, {
    mode: 0o600,
  });
  await rename(temporary, testSummaryPath);
}

await testDistribution();
