import { realpathSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";

import { resolveCodexExecutable } from "./codex.mjs";
import { DistributionError } from "./errors.mjs";

const missingPackageMessage =
  "compatible Aizim native package is missing; use macOS or glibc Linux on arm64/x64 and reinstall without --omit=optional";
const incompleteMessage =
  "Aizim distribution is incomplete; perform a clean reinstall";

function absolutePath(value) {
  return value instanceof URL ? fileURLToPath(value) : value;
}

function regularFile(candidate) {
  if (typeof candidate !== "string" || !isAbsolute(candidate)) {
    throw new Error("expected an absolute file path");
  }
  const resolved = realpathSync(candidate);
  if (!statSync(resolved).isFile()) {
    throw new Error("expected a regular file");
  }
  return resolved;
}

function directory(candidate) {
  if (typeof candidate !== "string" || !isAbsolute(candidate)) {
    throw new Error("expected an absolute directory path");
  }
  const resolved = realpathSync(candidate);
  if (!statSync(resolved).isDirectory()) {
    throw new Error("expected a directory");
  }
  return resolved;
}

export function resolveAssets(
  target,
  requireFromMeta = createRequire(import.meta.url),
  metaRoot = new URL("../", import.meta.url),
) {
  try {
    requireFromMeta.resolve(`${target.packageName}/package.json`);
  } catch (error) {
    throw new DistributionError(
      "PLATFORM_PACKAGE_MISSING",
      missingPackageMessage,
      { cause: error },
    );
  }

  let platformPackage;
  try {
    platformPackage = requireFromMeta(target.packageName);
  } catch (error) {
    throw new DistributionError(
      "DISTRIBUTION_INCOMPLETE",
      incompleteMessage,
      { cause: error },
    );
  }

  let resolved;
  try {
    const root = directory(absolutePath(metaRoot));
    resolved = {
      launcher: regularFile(platformPackage.launcher),
      platformManifest: regularFile(platformPackage.platformManifest),
      distributionManifest: regularFile(
        join(root, "manifest", "distribution.json"),
      ),
      vendorRoot: directory(join(root, "vendor")),
    };
  } catch (error) {
    throw new DistributionError(
      "DISTRIBUTION_INCOMPLETE",
      incompleteMessage,
      { cause: error },
    );
  }

  return Object.freeze({
    ...resolved,
    codexExecutable: regularFile(
      resolveCodexExecutable(target, requireFromMeta),
    ),
  });
}
