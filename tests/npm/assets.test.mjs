import assert from "node:assert/strict";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";

import { resolveAssets } from "../../lib/assets.mjs";
import { DistributionError } from "../../lib/errors.mjs";
import { detectTarget } from "../../lib/platform.mjs";

const target = detectTarget({ platform: "darwin", arch: "arm64" });

function fixture({ missingPlatform = false, missingFile } = {}) {
  const root = mkdtempSync(join(tmpdir(), "aizim-assets-"));
  writeFileSync(join(root, "package.json"), '{"private":true}\n');

  const distributionManifest = join(root, "manifest", "distribution.json");
  const vendorRoot = join(root, "vendor");
  mkdirSync(join(root, "manifest"), { recursive: true });
  mkdirSync(vendorRoot, { recursive: true });
  if (missingFile !== "distributionManifest") {
    writeFileSync(distributionManifest, "{}\n");
  }
  writeFileSync(join(vendorRoot, "aizim.whl"), "wheel\n");
  writeFileSync(join(vendorRoot, "runtime-requirements.txt"), "aizim==0.1.0\n");

  let launcher;
  let platformManifest;
  if (!missingPlatform) {
    const platformRoot = join(
      root,
      "node_modules",
      "@aiz.im",
      "aizim-darwin-arm64",
    );
    launcher = join(platformRoot, "bin", "aizim-launcher");
    platformManifest = join(platformRoot, "manifest", "platform.json");
    mkdirSync(join(platformRoot, "bin"), { recursive: true });
    mkdirSync(join(platformRoot, "manifest"), { recursive: true });
    writeFileSync(
      join(platformRoot, "package.json"),
      `${JSON.stringify({
        name: "@aiz.im/aizim-darwin-arm64",
        version: "0.1.0",
        main: "index.cjs",
      })}\n`,
    );
    writeFileSync(
      join(platformRoot, "index.cjs"),
      [
        '"use strict";',
        'const path = require("node:path");',
        "module.exports = Object.freeze({",
        '  launcher: path.join(__dirname, "bin", "aizim-launcher"),',
        '  platformManifest: path.join(__dirname, "manifest", "platform.json"),',
        "});",
        "",
      ].join("\n"),
    );
    if (missingFile !== "launcher") {
      writeFileSync(launcher, "#!/bin/sh\nexit 0\n");
      chmodSync(launcher, 0o755);
    }
    writeFileSync(platformManifest, "{}\n");
  }

  const codexRoot = join(root, "node_modules", "@openai", "codex");
  const codexNativeRoot = join(
    codexRoot,
    "node_modules",
    "@openai",
    "codex-darwin-arm64",
  );
  const codexExecutable = join(
    codexNativeRoot,
    "vendor",
    "aarch64-apple-darwin",
    "bin",
    "codex",
  );
  mkdirSync(join(codexExecutable, ".."), { recursive: true });
  writeFileSync(
    join(codexRoot, "package.json"),
    '{"name":"@openai/codex","version":"0.145.0"}\n',
  );
  writeFileSync(
    join(codexNativeRoot, "package.json"),
    '{"name":"@openai/codex-darwin-arm64","version":"0.145.0-darwin-arm64"}\n',
  );
  writeFileSync(codexExecutable, "#!/bin/sh\nexit 0\n");
  chmodSync(codexExecutable, 0o755);

  const claudeRoot = join(root, "node_modules", "@anthropic-ai", "claude-code");
  const claudeNativeRoot = join(
    claudeRoot,
    "node_modules",
    "@anthropic-ai",
    "claude-code-darwin-arm64",
  );
  const claudeExecutable = join(claudeNativeRoot, "claude");
  mkdirSync(claudeNativeRoot, { recursive: true });
  writeFileSync(
    join(claudeRoot, "package.json"),
    '{"name":"@anthropic-ai/claude-code","version":"2.1.218"}\n',
  );
  writeFileSync(
    join(claudeNativeRoot, "package.json"),
    '{"name":"@anthropic-ai/claude-code-darwin-arm64","version":"2.1.218"}\n',
  );
  writeFileSync(claudeExecutable, "#!/bin/sh\necho '2.1.218 (Claude Code)'\n");
  chmodSync(claudeExecutable, 0o755);

  return {
    cleanup: () => rmSync(root, { force: true, recursive: true }),
    claudeExecutable,
    codexExecutable,
    distributionManifest,
    launcher,
    metaRoot: root,
    platformManifest,
    requireFromMeta: createRequire(pathToFileURL(join(root, "package.json"))),
    vendorRoot,
  };
}

test("resolves the complete installed distribution from absolute paths", () => {
  // Given
  const tree = fixture();

  try {
    // When
    const assets = resolveAssets(
      target,
      tree.requireFromMeta,
      tree.metaRoot,
    );

    // Then
    assert.equal(assets.launcher, realpathSync(tree.launcher));
    assert.equal(
      assets.platformManifest,
      realpathSync(tree.platformManifest),
    );
    assert.equal(
      assets.distributionManifest,
      realpathSync(tree.distributionManifest),
    );
    assert.equal(assets.vendorRoot, realpathSync(tree.vendorRoot));
    assert.equal(
      assets.codexExecutable,
      realpathSync(tree.codexExecutable),
    );
    assert.equal(
      assets.claudeExecutable,
      realpathSync(tree.claudeExecutable),
    );
    assert.equal(Object.isFrozen(assets), true);
  } finally {
    tree.cleanup();
  }
});

test("reports a missing optional platform package", () => {
  // Given
  const tree = fixture({ missingPlatform: true });

  try {
    // When / Then
    assert.throws(
      () => resolveAssets(target, tree.requireFromMeta, tree.metaRoot),
      (error) =>
        error instanceof DistributionError &&
        error.code === "PLATFORM_PACKAGE_MISSING",
    );
  } finally {
    tree.cleanup();
  }
});

for (const missingFile of ["launcher", "distributionManifest"]) {
  test(`reports an incomplete distribution without ${missingFile}`, () => {
    // Given
    const tree = fixture({ missingFile });

    try {
      // When / Then
      assert.throws(
        () => resolveAssets(target, tree.requireFromMeta, tree.metaRoot),
        (error) =>
          error instanceof DistributionError &&
          error.code === "DISTRIBUTION_INCOMPLETE",
      );
    } finally {
      tree.cleanup();
    }
  });
}
