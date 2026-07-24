import assert from "node:assert/strict";
import test from "node:test";

import { pythonTestMarker } from "../../scripts/npm/test-python.mjs";

test("selects only the native sandbox tests for the current platform", () => {
  assert.equal(
    pythonTestMarker("darwin"),
    "not manual_real_codex and not manual_real_controller and not lean_integration and not linux_sandbox",
  );
  assert.equal(
    pythonTestMarker("linux"),
    "not manual_real_codex and not manual_real_controller and not lean_integration and not macos_sandbox",
  );
  assert.throws(() => pythonTestMarker("win32"), /unsupported npm test platform/u);
});
