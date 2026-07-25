import { spawn } from "node:child_process";

import { DistributionError } from "./errors.mjs";

const forwardedSignals = ["SIGINT", "SIGTERM", "SIGHUP"];
const configurationErrors = new Set([
  "UNSUPPORTED_PLATFORM",
  "PLATFORM_PACKAGE_MISSING",
  "DISTRIBUTION_INCOMPLETE",
]);

export async function launchAizim(
  assets,
  argv,
  processLike = process,
  spawnImpl = spawn,
) {
  const internalArgs = [
    "--distribution-manifest",
    assets.distributionManifest,
    "--platform-manifest",
    assets.platformManifest,
    "--",
    ...argv,
  ];

  return await new Promise((resolve, reject) => {
    let child;
    try {
      child = spawnImpl(assets.launcher, internalArgs, {
        env: processLike.env,
        shell: false,
        stdio: "inherit",
      });
    } catch (cause) {
      reject(
        new DistributionError(
          "LAUNCHER_UNAVAILABLE",
          "native Aizim launcher is unavailable",
          { cause },
        ),
      );
      return;
    }

    const sent = new Set();
    const handlers = new Map(
      forwardedSignals.map((signal) => [
        signal,
        () => {
          if (!sent.has(signal)) {
            sent.add(signal);
            child.kill(signal);
          }
        },
      ]),
    );
    for (const [signal, handler] of handlers) {
      processLike.on(signal, handler);
    }

    const cleanup = () => {
      for (const [signal, handler] of handlers) {
        processLike.off(signal, handler);
      }
    };

    child.once("error", (cause) => {
      cleanup();
      reject(
        new DistributionError(
          "LAUNCHER_UNAVAILABLE",
          "native Aizim launcher is unavailable",
          { cause },
        ),
      );
    });
    child.once("exit", (code, signal) => {
      cleanup();
      if (signal) {
        processLike.kill(processLike.pid, signal);
        resolve(70);
        return;
      }
      resolve(Number.isInteger(code) ? code : 70);
    });
  });
}

export function reportDistributionError(error, processLike = process) {
  const code = String(error.code).replaceAll(/[\r\n]/g, "");
  const message = error.message.replaceAll(/[\r\n]+/g, " ");
  processLike.stderr.write(`aizim: ${code}: ${message}\n`);
  return configurationErrors.has(error.code) ? 78 : 70;
}
