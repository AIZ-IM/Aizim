#!/usr/bin/env node

import { resolveAssets } from "../lib/assets.mjs";
import { DistributionError } from "../lib/errors.mjs";
import { launchAizim, reportDistributionError } from "../lib/launch.mjs";
import { detectTarget } from "../lib/platform.mjs";

try {
  const target = detectTarget();
  const assets = resolveAssets(
    target,
    undefined,
    new URL("../", import.meta.url),
  );
  process.exitCode = await launchAizim(
    assets,
    process.argv.slice(2),
    process,
  );
} catch (error) {
  if (!(error instanceof DistributionError)) {
    throw error;
  }
  process.exitCode = reportDistributionError(error, process);
}
