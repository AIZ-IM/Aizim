import assert from "node:assert/strict";
import test from "node:test";

import { pythonTestMarker } from "../../scripts/npm/test-python.mjs";

test("keeps external sandbox contracts out of provider-free package tests", () => {
  assert.equal(
    pythonTestMarker("darwin"),
    "not manual_real_codex and not manual_real_controller and not lean_integration and not linux_sandbox and not macos_sandbox",
  );
  assert.equal(
    pythonTestMarker("linux"),
    "not manual_real_codex and not manual_real_controller and not lean_integration and not linux_sandbox and not macos_sandbox",
  );
  assert.throws(() => pythonTestMarker("win32"), /unsupported npm test platform/u);
});
