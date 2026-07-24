import { mkdir } from "node:fs/promises";
import { join } from "node:path";

import { run } from "./lib/command.mjs";
import {
  npmBuildRoot,
  repositoryRoot,
  targetBuildPath,
} from "./lib/paths.mjs";
import { verifyFreshBuild } from "./pack.mjs";

const retainedEnvironment = new Set([
  "HOME",
  "HTTP_PROXY",
  "HTTPS_PROXY",
  "LANG",
  "LC_ALL",
  "NO_PROXY",
  "PATH",
  "SSL_CERT_DIR",
  "SSL_CERT_FILE",
  "TMPDIR",
]);

export function pythonTestEnvironment(source = process.env) {
  const environment = Object.fromEntries(
    Object.entries(source).filter(([name]) => retainedEnvironment.has(name)),
  );
  environment.UV_CACHE_DIR = join(npmBuildRoot, "test-uv-cache");
  environment.UV_NO_PROGRESS = "1";
  return environment;
}

export function pythonTestMarker(platform = process.platform) {
  if (platform === "darwin") {
    return "not manual_real_codex and not manual_real_controller and not lean_integration and not linux_sandbox";
  }
  if (platform === "linux") {
    return "not manual_real_codex and not manual_real_controller and not lean_integration and not macos_sandbox";
  }
  throw new Error(`unsupported npm test platform: ${platform}`);
}

export async function testPython() {
  const summary = await verifyFreshBuild();
  const uv = join(targetBuildPath(summary.target), "vendor", "uv");
  const environment = pythonTestEnvironment();
  await mkdir(environment.UV_CACHE_DIR, { mode: 0o700, recursive: true });
  for (const arguments_ of [
    ["--no-config", "sync", "--frozen"],
    ["--no-config", "run", "ruff", "check", "."],
    ["--no-config", "run", "ty", "check"],
    [
      "--no-config",
      "run",
      "pytest",
      "-m",
      pythonTestMarker(),
      "-q",
    ],
  ]) {
    await run(uv, arguments_, {
      cwd: repositoryRoot,
      env: environment,
    });
  }
}

if (process.argv[1] === new URL(import.meta.url).pathname) {
  await testPython();
}
