import {
  accessSync,
  constants,
  readFileSync,
  realpathSync,
  statSync,
} from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

import { DistributionError } from "./errors.mjs";

function readJson(file) {
  return JSON.parse(readFileSync(file, "utf8"));
}

export function resolveClaudeExecutable(
  target,
  requireFromMeta = createRequire(import.meta.url),
) {
  try {
    const metaPath = requireFromMeta.resolve(
      "@anthropic-ai/claude-code/package.json",
    );
    const meta = readJson(metaPath);
    if (meta.version !== "2.1.218") {
      throw new Error("meta version mismatch");
    }

    const nativePath = createRequire(metaPath).resolve(
      `${target.claudeAlias}/package.json`,
    );
    const native = readJson(nativePath);
    if (native.version !== target.claudeVersion) {
      throw new Error("native version mismatch");
    }

    const executable = realpathSync(join(dirname(nativePath), "claude"));
    if (!statSync(executable).isFile()) {
      throw new Error("native executable is not a regular file");
    }
    accessSync(executable, constants.X_OK);
    return executable;
  } catch (error) {
    throw new DistributionError(
      "CLAUDE_PACKAGE_INVALID",
      "local Claude Code 2.1.218 native package is unavailable",
      { cause: error },
    );
  }
}
