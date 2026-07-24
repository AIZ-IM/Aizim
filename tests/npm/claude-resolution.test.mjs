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

import { resolveClaudeExecutable } from "../../lib/claude.mjs";
import { DistributionError } from "../../lib/errors.mjs";
import { reportDistributionError } from "../../lib/launch.mjs";
import { detectTarget } from "../../lib/platform.mjs";

const target = detectTarget({ platform: "darwin", arch: "arm64" });

function fixture({
  metaVersion = "2.1.218",
  nativeVersion = "2.1.218",
  includeNative = true,
  includeBinary = true,
  executable = true,
  binaryDirectory = false,
} = {}) {
  const root = mkdtempSync(join(tmpdir(), "aizim-claude-"));
  const metaRoot = join(root, "node_modules", "@anthropic-ai", "claude-code");
  const nativeRoot = join(
    metaRoot,
    "node_modules",
    "@anthropic-ai",
    "claude-code-darwin-arm64",
  );
  const fallbackRoot = join(root, "path-fallback");
  mkdirSync(metaRoot, { recursive: true });
  mkdirSync(fallbackRoot, { recursive: true });
  writeFileSync(join(root, "package.json"), '{"private":true}\n');
  writeFileSync(
    join(metaRoot, "package.json"),
    `${JSON.stringify({
      name: "@anthropic-ai/claude-code",
      version: metaVersion,
    })}\n`,
  );
  const fallback = join(fallbackRoot, "claude");
  writeFileSync(fallback, "#!/bin/sh\necho fallback\n");
  chmodSync(fallback, 0o755);

  let binary;
  if (includeNative) {
    mkdirSync(nativeRoot, { recursive: true });
    writeFileSync(
      join(nativeRoot, "package.json"),
      `${JSON.stringify({
        name: "@anthropic-ai/claude-code-darwin-arm64",
        version: nativeVersion,
      })}\n`,
    );
    if (includeBinary) {
      binary = join(nativeRoot, "claude");
      if (binaryDirectory) {
        mkdirSync(binary);
      } else {
        writeFileSync(binary, "#!/bin/sh\necho '2.1.218 (Claude Code)'\n");
      }
      chmodSync(binary, executable ? 0o755 : 0o644);
    }
  }

  return {
    binary,
    cleanup: () => rmSync(root, { force: true, recursive: true }),
    fallbackRoot,
    requireFromMeta: createRequire(pathToFileURL(join(root, "package.json"))),
  };
}

test("resolves the exact nested native Claude executable", () => {
  // Given
  const tree = fixture();

  try {
    // When
    const executable = resolveClaudeExecutable(target, tree.requireFromMeta);

    // Then
    assert.equal(executable, realpathSync(tree.binary));
  } finally {
    tree.cleanup();
  }
});

for (const [name, options] of [
  ["wrong meta version", { metaVersion: "2.1.219" }],
  ["wrong native version", { nativeVersion: "2.1.217" }],
  ["missing native alias", { includeNative: false }],
  ["missing executable", { includeBinary: false }],
  ["non-executable file", { executable: false }],
]) {
  test(`rejects ${name} without a PATH fallback`, () => {
    // Given
    const tree = fixture(options);
    const previousPath = process.env.PATH;
    process.env.PATH = tree.fallbackRoot;

    try {
      // When / Then
      assert.throws(
        () => resolveClaudeExecutable(target, tree.requireFromMeta),
        (error) =>
          error instanceof DistributionError &&
          error.code === "CLAUDE_PACKAGE_INVALID" &&
          !error.message.includes(tree.fallbackRoot),
      );
    } finally {
      process.env.PATH = previousPath;
      tree.cleanup();
    }
  });
}

test("rejects an executable directory as a typed configuration error", () => {
  // Given
  const tree = fixture({ binaryDirectory: true });
  const stderrLines = [];

  try {
    // When / Then
    assert.throws(
      () => resolveClaudeExecutable(target, tree.requireFromMeta),
      (error) => {
        assert.equal(
          reportDistributionError(error, {
            stderr: { write: (line) => stderrLines.push(line) },
          }),
          78,
        );
        assert.deepEqual(stderrLines, [
          "aizim: CLAUDE_PACKAGE_INVALID: local Claude Code 2.1.218 native package is unavailable\n",
        ]);
        return (
          error instanceof DistributionError &&
          error.code === "CLAUDE_PACKAGE_INVALID"
        );
      },
    );
  } finally {
    tree.cleanup();
  }
});
