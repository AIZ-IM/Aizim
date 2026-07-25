import {
  chmod,
  copyFile,
  lstat,
  mkdir,
  readdir,
} from "node:fs/promises";
import {
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
  sep,
} from "node:path";

import {
  metaBuildPath,
  npmBuildRoot,
  repositoryRoot,
  targetBuildPath,
} from "./lib/paths.mjs";

export const META_ALLOWLIST = Object.freeze([
  "LICENSE",
  "README.md",
  "THIRD_PARTY_NOTICES.md",
  "bin/aizim.js",
  "lib/assets.mjs",
  "lib/errors.mjs",
  "lib/launch.mjs",
  "lib/platform.mjs",
  "manifest/distribution.json",
  "package.json",
  "vendor/aizim-0.1.0-py3-none-any.whl",
  "vendor/runtime-requirements.txt",
]);

export const PLATFORM_ALLOWLIST = Object.freeze([
  "LICENSE",
  "README.md",
  "THIRD_PARTY_NOTICES.md",
  "bin/aizim-launcher",
  "index.cjs",
  "manifest/platform.json",
  "package.json",
  "vendor/licenses/uv/LICENSE-APACHE",
  "vendor/licenses/uv/LICENSE-MIT",
  "vendor/uv",
]);

function isContained(root, path) {
  const fromRoot = relative(root, path);
  return (
    fromRoot === "" ||
    (!isAbsolute(fromRoot) &&
      fromRoot !== ".." &&
      !fromRoot.startsWith(`..${sep}`))
  );
}

async function packageFiles(root, current = root, output = []) {
  for (const entry of await readdir(current, { withFileTypes: true })) {
    const path = join(current, entry.name);
    const metadata = await lstat(path);
    if (metadata.isSymbolicLink()) {
      throw new Error("package tree contains a symlink");
    }
    if (metadata.isDirectory()) {
      await packageFiles(root, path, output);
      continue;
    }
    if (!metadata.isFile() || metadata.nlink !== 1) {
      throw new Error("package tree contains an unsafe file");
    }
    output.push(relative(root, path).split(sep).join("/"));
  }
  return output;
}

export async function validatePackageTree(root, allowlist) {
  const actual = (await packageFiles(root)).sort();
  const expected = [...allowlist].sort();
  if (
    actual.length !== expected.length ||
    actual.some((path, index) => path !== expected[index])
  ) {
    throw new Error("package tree allowlist mismatch");
  }
}

async function copyPackageFile(source, destination, mode) {
  const sourcePath = resolve(source);
  const destinationPath = resolve(destination);
  if (
    (!isContained(repositoryRoot, sourcePath) &&
      !isContained(npmBuildRoot, sourcePath)) ||
    !isContained(npmBuildRoot, destinationPath)
  ) {
    throw new Error("package copy escapes declared roots");
  }
  const metadata = await lstat(sourcePath);
  if (
    !metadata.isFile() ||
    metadata.isSymbolicLink() ||
    metadata.nlink !== 1
  ) {
    throw new Error("package source is unsafe");
  }
  await mkdir(dirname(destinationPath), { mode: 0o700, recursive: true });
  if (sourcePath !== destinationPath) {
    await copyFile(sourcePath, destinationPath);
  }
  await chmod(destinationPath, mode);
}

export async function assemblePackages(target, artifacts) {
  const platformRoot = targetBuildPath(target);
  const metaCopies = [
    ["package.json", "package.json", 0o644],
    ["bin/aizim.js", "bin/aizim.js", 0o755],
    ["lib/assets.mjs", "lib/assets.mjs", 0o644],
    ["lib/errors.mjs", "lib/errors.mjs", 0o644],
    ["lib/launch.mjs", "lib/launch.mjs", 0o644],
    ["lib/platform.mjs", "lib/platform.mjs", 0o644],
    ["LICENSE", "LICENSE", 0o644],
    ["npm/README.md", "README.md", 0o644],
    ["THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md", 0o644],
  ];
  for (const [source, destination, mode] of metaCopies) {
    await copyPackageFile(
      join(repositoryRoot, source),
      join(metaBuildPath, destination),
      mode,
    );
  }
  for (const [source, destination, mode] of [
    [artifacts.distributionPath, join(metaBuildPath, "manifest/distribution.json"), 0o644],
    [artifacts.wheel, join(metaBuildPath, "vendor/aizim-0.1.0-py3-none-any.whl"), 0o644],
    [artifacts.requirements, join(metaBuildPath, "vendor/runtime-requirements.txt"), 0o644],
  ]) {
    await copyPackageFile(source, destination, mode);
  }

  const platformCopies = [
    [
      `npm/platforms/${target}/package.publish.json`,
      "package.json",
      0o644,
    ],
    [`npm/platforms/${target}/index.cjs`, "index.cjs", 0o644],
    ["LICENSE", "LICENSE", 0o644],
    ["npm/README.md", "README.md", 0o644],
    ["THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md", 0o644],
    [
      "third_party/uv/LICENSE-APACHE",
      "vendor/licenses/uv/LICENSE-APACHE",
      0o644,
    ],
    [
      "third_party/uv/LICENSE-MIT",
      "vendor/licenses/uv/LICENSE-MIT",
      0o644,
    ],
  ];
  for (const [source, destination, mode] of platformCopies) {
    await copyPackageFile(
      join(repositoryRoot, source),
      join(platformRoot, destination),
      mode,
    );
  }
  for (const [source, destination, mode] of [
    [artifacts.platformPath, join(platformRoot, "manifest/platform.json"), 0o644],
    [artifacts.launcher, join(platformRoot, "bin/aizim-launcher"), 0o755],
    [artifacts.uv, join(platformRoot, "vendor/uv"), 0o755],
  ]) {
    await copyPackageFile(source, destination, mode);
  }

  await validatePackageTree(metaBuildPath, META_ALLOWLIST);
  await validatePackageTree(platformRoot, PLATFORM_ALLOWLIST);
}
