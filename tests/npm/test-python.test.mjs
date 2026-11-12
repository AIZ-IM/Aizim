import assert from "node:assert/strict";
import test from "node:test";

import { pythonTestMarker } from "../../scripts/npm/test-python.mjs";

test("keeps Lean and sandbox integration out of package tests", () => {
  assert.equal(
    pythonTestMarker("darwin"),
    "not lean_integration and not linux_sandbox and not macos_sandbox",
  );
  assert.equal(
    pythonTestMarker("linux"),
    "not lean_integration and not linux_sandbox and not macos_sandbox",
  );
  assert.throws(() => pythonTestMarker("win32"), /unsupported npm test platform/u);
});
