import { readdir, stat } from "node:fs/promises";
import {
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
  sep,
} from "node:path";
import { fileURLToPath } from "node:url";

export const repositoryRoot = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../..",
);
export const npmBuildRoot = join(repositoryRoot, "build", "npm");
export const npmDistRoot = join(repositoryRoot, "dist", "npm");
export const metaBuildPath = join(npmBuildRoot, "meta");
export const wheelBuildPath = join(npmBuildRoot, "wheel");
export const requirementsBuildPath = join(
  npmBuildRoot,
  "runtime-requirements.txt",
);
export const buildSummaryPath = join(npmBuildRoot, "build-summary.json");

const declaredSourceRoots = [
  "Cargo.lock",
  "Cargo.toml",
  "LICENSE",
  "README.md",
  "THIRD_PARTY_NOTICES.md",
  "bin",
  "crates/aizim-launcher/Cargo.toml",
  "crates/aizim-launcher/src",
  "lib",
  "npm",
  "package-lock.json",
  "package.json",
  "pyproject.toml",
  "rust-toolchain.toml",
  "scripts/npm",
  "src",
  "third_party/uv",
  "uv.lock",
];

function containedBy(root, path) {
  const fromRoot = relative(root, path);
  return (
    fromRoot === "" ||
    (!isAbsolute(fromRoot) &&
      fromRoot !== ".." &&
      !fromRoot.startsWith(`..${sep}`))
  );
}

export function assertGeneratedPath(path) {
  const absolute = resolve(path);
  if (
    !containedBy(npmBuildRoot, absolute) &&
    !containedBy(npmDistRoot, absolute)
  ) {
    throw new Error("generated path is outside npm build roots");
  }
  return absolute;
}

export function targetBuildPath(target) {
  return assertGeneratedPath(join(npmBuildRoot, target));
}

export function uvArchivePath(target, archive) {
  return assertGeneratedPath(join(npmBuildRoot, "tools", target, archive));
}

export function uvExtractionPath(target) {
  return assertGeneratedPath(join(npmBuildRoot, "tools", target, "extract"));
}

export function uvToolPath(target) {
  return assertGeneratedPath(join(npmBuildRoot, "tools", target, "uv"));
}

async function collectFiles(root, path, output) {
  const metadata = await stat(path);
  if (metadata.isFile()) {
    output.push(relative(root, path));
    return;
  }
  if (!metadata.isDirectory()) {
    throw new Error("declared source input is not a file or directory");
  }
  const entries = await readdir(path, { withFileTypes: true });
  for (const entry of entries.sort((left, right) =>
    left.name.localeCompare(right.name),
  )) {
    if (entry.isSymbolicLink()) {
      throw new Error("declared source input contains a symlink");
    }
    if (entry.isDirectory() && entry.name === "__pycache__") {
      continue;
    }
    await collectFiles(root, join(path, entry.name), output);
  }
}

export async function sourceInputPaths(
  root = repositoryRoot,
  sources = declaredSourceRoots,
) {
  const rootPath = resolve(root);
  const output = [];
  for (const source of sources) {
    const path = join(rootPath, source);
    try {
      await collectFiles(rootPath, path, output);
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw error;
      }
    }
  }
  return output.sort();
}
