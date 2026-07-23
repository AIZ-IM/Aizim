import {
  createHash,
  timingSafeEqual,
} from "node:crypto";
import { createReadStream } from "node:fs";
import { lstat } from "node:fs/promises";
import { isAbsolute, relative, resolve, sep } from "node:path";

const digestPattern = /^[0-9a-f]{64}$/;

async function updateWithFile(hash, path) {
  for await (const chunk of createReadStream(path)) {
    hash.update(chunk);
  }
}

export async function sha256File(path) {
  const hash = createHash("sha256");
  await updateWithFile(hash, path);
  return hash.digest("hex");
}

export function assertDigest(actual, expected, compare = timingSafeEqual) {
  if (!digestPattern.test(actual) || !digestPattern.test(expected)) {
    throw new Error("invalid lowercase SHA-256 digest");
  }
  if (!compare(Buffer.from(actual, "hex"), Buffer.from(expected, "hex"))) {
    throw new Error("SHA-256 digest mismatch");
  }
}

export async function artifactRecord(path, manifestPath) {
  const metadata = await lstat(path);
  if (!metadata.isFile() || metadata.isSymbolicLink()) {
    throw new Error("artifact must be a regular file");
  }
  return {
    path: manifestPath,
    size: metadata.size,
    sha256: await sha256File(path),
  };
}

export async function hashInputPaths(root, paths) {
  const hash = createHash("sha256");
  const rootPath = resolve(root);
  const normalized = [...new Set(paths)].sort();

  for (const path of normalized) {
    if (isAbsolute(path)) {
      throw new Error("source input path must be relative");
    }
    const absolute = resolve(rootPath, path);
    const fromRoot = relative(rootPath, absolute);
    if (
      fromRoot === ".." ||
      fromRoot.startsWith(`..${sep}`) ||
      isAbsolute(fromRoot)
    ) {
      throw new Error("source input path escapes repository");
    }
    const metadata = await lstat(absolute);
    if (!metadata.isFile() || metadata.isSymbolicLink()) {
      throw new Error("source input must be a regular file");
    }
    hash.update(path);
    hash.update("\0");
    await updateWithFile(hash, absolute);
    hash.update("\0");
  }
  return hash.digest("hex");
}
