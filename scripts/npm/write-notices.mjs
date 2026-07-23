import { writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { capture } from "./lib/command.mjs";
import { repositoryRoot } from "./lib/paths.mjs";

const allowedLicenses = new Set([
  "Apache-2.0",
  "Apache-2.0 OR MIT",
  "Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT",
  "ISC",
  "MIT",
  "MIT OR Apache-2.0",
  "MIT OR Apache-2.0 OR LGPL-2.1-or-later",
  "MIT/Apache-2.0",
  "Unicode-3.0",
  "Unlicense OR MIT",
  "(MIT OR Apache-2.0) AND Unicode-3.0",
]);

function releaseDependencyIds(metadata) {
  const root = metadata.packages.find(
    (candidate) =>
      candidate.name === "aizim-launcher" && candidate.source === null,
  );
  if (!root || !metadata.resolve) {
    throw new Error("launcher dependency graph is missing");
  }
  const nodes = new Map(
    metadata.resolve.nodes.map((node) => [node.id, node]),
  );
  const selected = new Set();
  const pending = [root.id];
  while (pending.length > 0) {
    const id = pending.pop();
    if (selected.has(id)) {
      continue;
    }
    selected.add(id);
    for (const dependency of nodes.get(id)?.deps ?? []) {
      if (dependency.dep_kinds.some(({ kind }) => kind !== "dev")) {
        pending.push(dependency.pkg);
      }
    }
  }
  selected.delete(root.id);
  return selected;
}

export function generateNotices(metadata) {
  const selected = releaseDependencyIds(metadata);
  const packages = metadata.packages
    .filter(({ id }) => selected.has(id))
    .sort(
      (left, right) =>
        left.name.localeCompare(right.name) ||
        left.version.localeCompare(right.version),
    );
  const lines = ["Aizim Third-Party Notices", "", "uv 0.11.31 — MIT OR Apache-2.0"];
  for (const dependency of packages) {
    if (
      !allowedLicenses.has(dependency.license) ||
      /[\r\n]/u.test(dependency.name) ||
      /[\r\n]/u.test(dependency.version)
    ) {
      throw new Error("Rust dependency license is not allowlisted");
    }
    lines.push(
      `${dependency.name} ${dependency.version} — ${dependency.license}`,
    );
  }
  return `${lines.join("\n")}\n`;
}

export async function currentNotices() {
  const raw = await capture(
    "cargo",
    ["metadata", "--format-version", "1", "--locked"],
    { cwd: repositoryRoot },
  );
  return generateNotices(JSON.parse(raw));
}

export async function writeNotices() {
  const notices = await currentNotices();
  await writeFile(join(repositoryRoot, "THIRD_PARTY_NOTICES.md"), notices, {
    mode: 0o644,
  });
  return notices;
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : undefined;
if (invokedPath === fileURLToPath(import.meta.url)) {
  await writeNotices();
}
