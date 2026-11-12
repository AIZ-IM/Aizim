import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const latestActions = [
  "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
  "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1",
  "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
  "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333",
];
const runners = [
  "macos-15",
  "ubuntu-24.04-arm",
  "ubuntu-24.04",
];
const forbidden =
  /npm publish|id-token:\s*write|NODE_AUTH_TOKEN|NPM_TOKEN|npm-token|registry-token/iu;

test("release workflow is manual-only, exact-commit, three-target, and non-publishing", async () => {
  const workflow = await readFile(
    new URL("../../.github/workflows/npm-release.yml", import.meta.url),
    "utf8",
  );
  for (const value of [
    "workflow_dispatch:",
    "version:",
    "commit_sha:",
    "permissions:",
    "contents: read",
    "ref: ${{ inputs.commit_sha }}",
    "verify-release-bundle.mjs",
    "dirty_tracked_inputs",
    "restored_build_cache",
    'npm install --global --prefix "$RUNNER_TEMP/npm-12" npm@12.2.2',
    'test "$(node --version)" = "v26.13.0"',
    ...runners,
    ...latestActions,
  ]) {
    assert.ok(workflow.includes(value), `missing release contract: ${value}`);
  }
  assert.doesNotMatch(workflow, /^\s+(?:push|pull_request|release):/gmu);
  assert.doesNotMatch(workflow, forbidden);
  assert.equal(
    workflow.match(/actions\/setup-node@/gu)?.length,
    workflow.match(/package-manager-cache:\s*false/gu)?.length,
  );
  assert.ok(
    workflow.includes(
      'echo "TMPDIR=$RUNNER_TEMP/aizim-private-tmp" >> "$GITHUB_ENV"',
    ),
  );
  assert.match(workflow, /name: Create private temporary root/u);
  for (const line of workflow.match(/^\s*uses:\s*.+$/gmu) ?? []) {
    assert.match(line, /@[0-9a-f]{40}(?:\s+#\s+v\d+\.\d+\.\d+)?$/u);
  }
});

test("registry smoke workflow is manual-only, three-target, read-only, and exact-version", async () => {
  const workflow = await readFile(
    new URL(
      "../../.github/workflows/npm-registry-smoke.yml",
      import.meta.url,
    ),
    "utf8",
  );
  for (const value of [
    "workflow_dispatch:",
    "version:",
    "permissions:",
    "contents: read",
    "registry-smoke.mjs",
    "--version",
    "--aggregate",
    'npm install --global --prefix "$RUNNER_TEMP/npm-12" npm@12.2.2',
    'test "$(node --version)" = "v26.13.0"',
    ...runners,
    ...latestActions,
  ]) {
    assert.ok(
      workflow.includes(value),
      `missing registry smoke contract: ${value}`,
    );
  }
  assert.doesNotMatch(workflow, /^\s+(?:push|pull_request|release):/gmu);
  assert.doesNotMatch(workflow, forbidden);
  assert.equal(
    workflow.match(/actions\/setup-node@/gu)?.length,
    workflow.match(/package-manager-cache:\s*false/gu)?.length,
  );
  assert.ok(
    workflow.includes(
      'echo "TMPDIR=$RUNNER_TEMP/aizim-private-tmp" >> "$GITHUB_ENV"',
    ),
  );
  assert.match(workflow, /name: Create private temporary root/u);
  for (const line of workflow.match(/^\s*uses:\s*.+$/gmu) ?? []) {
    assert.match(line, /@[0-9a-f]{40}(?:\s+#\s+v\d+\.\d+\.\d+)?$/u);
  }
});
