import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const latestActions = [
  "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
  "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020",
  "leanprover/lean-action@38fbc41a8c28c4cbaec22d7f7de508ec2e7c0dd9",
  "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
  "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
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
