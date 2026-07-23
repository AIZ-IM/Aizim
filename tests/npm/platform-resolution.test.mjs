import assert from "node:assert/strict";
import test from "node:test";

import { DistributionError } from "../../lib/errors.mjs";
import { detectTarget } from "../../lib/platform.mjs";

const glibc = { header: { glibcVersionRuntime: "2.39" } };

for (const [platform, arch, report, id] of [
  ["darwin", "arm64", undefined, "darwin-arm64"],
  ["darwin", "x64", undefined, "darwin-x64"],
  ["linux", "arm64", glibc, "linux-arm64"],
  ["linux", "x64", glibc, "linux-x64"],
]) {
  test(`maps ${platform} ${arch} to ${id}`, () => {
    // Given / When
    const target = detectTarget({ platform, arch, report });

    // Then
    assert.equal(target.id, id);
    assert.equal(Object.isFrozen(target), true);
  });
}

for (const input of [
  { platform: "win32", arch: "x64" },
  { platform: "darwin", arch: "ia32" },
  { platform: "linux", arch: "x64", report: { header: {} } },
]) {
  test(`rejects ${JSON.stringify(input)}`, () => {
    assert.throws(
      () => detectTarget(input),
      (error) =>
        error instanceof DistributionError &&
        error.code === "UNSUPPORTED_PLATFORM",
    );
  });
}
