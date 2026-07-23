import { randomUUID } from "node:crypto";
import {
  chmod,
  copyFile,
  lstat,
  mkdir,
  open,
  readFile,
  rename,
  rm,
} from "node:fs/promises";
import { dirname, join } from "node:path";
import { Readable } from "node:stream";

import { assertDigest, sha256File } from "./lib/hash.mjs";
import {
  uvArchivePath,
  uvExtractionPath,
  uvToolPath,
} from "./lib/paths.mjs";
import { run } from "./lib/command.mjs";

const artifacts = JSON.parse(
  await readFile(new URL("./uv-artifacts.json", import.meta.url), "utf8"),
);

async function downloadArchive(url, destination, fetchImpl) {
  const temporary = `${destination}.tmp-${process.pid}-${randomUUID()}`;
  let handle;
  try {
    const response = await fetchImpl(url, { redirect: "follow" });
    if (!response.ok || response.body === null) {
      throw new Error("uv archive download failed");
    }
    await mkdir(dirname(destination), { mode: 0o700, recursive: true });
    handle = await open(temporary, "wx", 0o600);
    for await (const chunk of Readable.fromWeb(response.body)) {
      await handle.writeFile(chunk);
    }
    await handle.sync();
    await handle.close();
    handle = undefined;
    await rename(temporary, destination);
  } catch (error) {
    await handle?.close();
    await rm(temporary, { force: true });
    throw error;
  }
}

async function requireVerifiedArchive(path, expected) {
  try {
    assertDigest(await sha256File(path), expected);
  } catch (error) {
    if (error?.code === "ENOENT") {
      throw error;
    }
    await rm(path, { force: true });
    throw new Error("uv archive digest mismatch");
  }
}

async function requireExecutable(path) {
  const metadata = await lstat(path);
  if (
    !metadata.isFile() ||
    metadata.isSymbolicLink() ||
    (metadata.mode & 0o111) === 0
  ) {
    throw new Error("uv executable is invalid");
  }
}

export async function fetchUv({
  target,
  artifact = artifacts.targets[target],
  version = artifacts.version,
  archivePath = uvArchivePath(target, artifact.archive),
  binaryPath = uvToolPath(target),
  extractionPath = uvExtractionPath(target),
  fetchImpl = globalThis.fetch,
  runImpl = run,
} = {}) {
  if (!artifact) {
    throw new Error("unsupported uv target");
  }
  try {
    await requireVerifiedArchive(archivePath, artifact.sha256);
  } catch (error) {
    if (error?.code !== "ENOENT") {
      throw error;
    }
    const url =
      `https://github.com/astral-sh/uv/releases/download/` +
      `${version}/${artifact.archive}`;
    await downloadArchive(url, archivePath, fetchImpl);
    await requireVerifiedArchive(archivePath, artifact.sha256);
  }

  await rm(extractionPath, { force: true, recursive: true });
  await mkdir(extractionPath, { mode: 0o700, recursive: true });
  await runImpl("tar", ["-xzf", archivePath, "-C", extractionPath]);
  const extracted = join(extractionPath, artifact.directory, "uv");
  try {
    await requireExecutable(extracted);
  } catch {
    await rm(extractionPath, { force: true, recursive: true });
    throw new Error("uv executable is invalid");
  }

  await mkdir(dirname(binaryPath), { mode: 0o700, recursive: true });
  const temporary = `${binaryPath}.tmp-${process.pid}-${randomUUID()}`;
  try {
    await copyFile(extracted, temporary);
    await chmod(temporary, 0o755);
    await requireExecutable(temporary);
    await rename(temporary, binaryPath);
  } finally {
    await rm(temporary, { force: true });
    await rm(extractionPath, { force: true, recursive: true });
  }
  return {
    path: binaryPath,
    sha256: await sha256File(binaryPath),
  };
}
