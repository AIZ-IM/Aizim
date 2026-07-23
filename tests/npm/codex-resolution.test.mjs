import assert from "node:assert/strict";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";

import { resolveCodexExecutable } from "../../lib/codex.mjs";
import { DistributionError } from "../../lib/errors.mjs";
import { detectTarget } from "../../lib/platform.mjs";

const target = detectTarget({ platform: "darwin", arch: "arm64" });

function fixture({
  metaVersion = "0.145.0",
  nativeVersion = "0.145.0-darwin-arm64",
  includeNative = true,
  includeBinary = true,
  executable = true,
} = {}) {
  const root = mkdtempSync(join(tmpdir(), "aizim-codex-"));
  const metaRoot = join(root, "node_modules", "@openai", "codex");
  const nativeRoot = join(
    metaRoot,
    "node_modules",
    "@openai",
    "codex-darwin-arm64",
  );
  mkdirSync(metaRoot, { recursive: true });
  writeFileSync(join(root, "package.json"), '{"private":true}\n');
  writeFileSync(
    join(metaRoot, "package.json"),
    `${JSON.stringify({ name: "@openai/codex", version: metaVersion })}\n`,
  );

  let binary;
  if (includeNative) {
    mkdirSync(nativeRoot, { recursive: true });
    writeFileSync(
      join(nativeRoot, "package.json"),
      `${JSON.stringify({
        name: "@openai/codex-darwin-arm64",
        version: nativeVersion,
      })}\n`,
    );
    if (includeBinary) {
      binary = join(
        nativeRoot,
        "vendor",
        "aarch64-apple-darwin",
        "bin",
        "codex",
      );
      mkdirSync(join(binary, ".."), { recursive: true });
      writeFileSync(binary, "#!/bin/sh\nexit 0\n");
      chmodSync(binary, executable ? 0o755 : 0o644);
    }
  }

  return {
    binary,
    cleanup: () => rmSync(root, { force: true, recursive: true }),
    requireFromMeta: createRequire(pathToFileURL(join(root, "package.json"))),
  };
}

test("resolves the exact nested native Codex executable", () => {
  // Given
  const tree = fixture();

  try {
    // When
    const executable = resolveCodexExecutable(target, tree.requireFromMeta);

    // Then
    assert.equal(executable, realpathSync(tree.binary));
  } finally {
    tree.cleanup();
  }
});

for (const [name, options] of [
  ["wrong meta version", { metaVersion: "0.145.1" }],
  ["wrong native version", { nativeVersion: "0.145.0-darwin-x64" }],
  ["missing native alias", { includeNative: false }],
  ["missing executable", { includeBinary: false }],
  ["non-executable file", { executable: false }],
]) {
  test(`rejects ${name} without a PATH fallback`, () => {
    // Given
    const tree = fixture(options);

    try {
      // When / Then
      assert.throws(
        () => resolveCodexExecutable(target, tree.requireFromMeta),
        (error) =>
          error instanceof DistributionError &&
          error.code === "CODEX_PACKAGE_INVALID" &&
          !error.message.includes(process.env.PATH ?? ""),
      );
    } finally {
      tree.cleanup();
    }
  });
}
