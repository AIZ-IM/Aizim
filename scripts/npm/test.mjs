import { run } from "./lib/command.mjs";
import { repositoryRoot } from "./lib/paths.mjs";
import { verifyFreshBuild } from "./pack.mjs";

async function testDistribution() {
  await verifyFreshBuild();
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
}

await testDistribution();
