import {
  accessSync,
  constants,
  readFileSync,
  realpathSync,
} from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

import { DistributionError } from "./errors.mjs";

function readJson(file) {
  return JSON.parse(readFileSync(file, "utf8"));
}

export function resolveCodexExecutable(
  target,
  requireFromMeta = createRequire(import.meta.url),
) {
  try {
    const metaPath = requireFromMeta.resolve("@openai/codex/package.json");
    const meta = readJson(metaPath);
    if (meta.version !== "0.145.0") {
      throw new Error("meta version mismatch");
    }

    const codexRequire = createRequire(metaPath);
    const nativePackagePath = codexRequire.resolve(
      `${target.codexAlias}/package.json`,
    );
    const nativePackage = readJson(nativePackagePath);
    if (nativePackage.version !== target.codexVersion) {
      throw new Error("native version mismatch");
    }

    const executable = realpathSync(
      join(
        dirname(nativePackagePath),
        "vendor",
        target.codexTriple,
        "bin",
        "codex",
      ),
    );
    accessSync(executable, constants.X_OK);
    return executable;
  } catch (error) {
    throw new DistributionError(
      "CODEX_PACKAGE_INVALID",
      "local Codex 0.145.0 native package is unavailable",
      { cause: error },
    );
  }
}
