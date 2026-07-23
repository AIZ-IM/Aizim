import { readFile } from "node:fs/promises";
import { join } from "node:path";

import { currentNotices } from "./write-notices.mjs";
import { run } from "./lib/command.mjs";
import { assertDigest, sha256File } from "./lib/hash.mjs";
import {
  repositoryRoot,
  targetBuildPath,
} from "./lib/paths.mjs";
import { verifyFreshBuild } from "./pack.mjs";

async function verifyUvLicenses() {
  const root = join(repositoryRoot, "third_party", "uv");
  const records = (
    await readFile(join(root, "SHA256SUMS"), "utf8")
  )
    .trimEnd()
    .split("\n");
  if (records.length !== 2) {
    throw new Error("uv license checksum table mismatch");
  }
  const expectedNames = ["LICENSE-APACHE", "LICENSE-MIT"];
  for (const [index, record] of records.entries()) {
    const match = record.match(/^([0-9a-f]{64}) {2}([A-Z-]+)$/);
    if (!match || match[2] !== expectedNames[index]) {
      throw new Error("uv license checksum table mismatch");
    }
    assertDigest(await sha256File(join(root, match[2])), match[1]);
  }
}

async function verifyNotices() {
  const committed = await readFile(
    join(repositoryRoot, "THIRD_PARTY_NOTICES.md"),
    "utf8",
  );
  if (committed !== (await currentNotices())) {
    throw new Error("third-party notices are stale");
  }
}

export async function check() {
  await verifyUvLicenses();
  await verifyNotices();
  const summary = await verifyFreshBuild();
  const uv = join(targetBuildPath(summary.target), "vendor", "uv");
  await run(
    uv,
    ["--no-config", "run", "--frozen", "--no-sync", "ruff", "check", "."],
    { cwd: repositoryRoot },
  );
  await run(
    uv,
    ["--no-config", "run", "--frozen", "--no-sync", "ty", "check"],
    { cwd: repositoryRoot },
  );
  await run("cargo", ["fmt", "--all", "--check"], {
    cwd: repositoryRoot,
  });
  process.stdout.write(`Checked npm distribution for ${summary.target}\n`);
}

await check();
