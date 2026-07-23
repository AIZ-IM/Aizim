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
      "not manual_real_codex",
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
