import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { DistributionError } from "../../lib/errors.mjs";
import {
  launchAizim,
  reportDistributionError,
} from "../../lib/launch.mjs";

const assets = Object.freeze({
  launcher: "/distribution/bin/aizim-launcher",
  distributionManifest: "/distribution/manifest/distribution.json",
  platformManifest: "/platform/manifest/platform.json",
});

class FakeProcess extends EventEmitter {
  constructor() {
    super();
    this.env = { AIZIM_TEST: "1" };
    this.pid = 314;
    this.kills = [];
    this.stderrLines = [];
    this.stderr = {
      write: (value) => {
        this.stderrLines.push(value);
      },
    };
  }

  kill(pid, signal) {
    this.kills.push({
      listeners: this.listenerCount(signal),
      pid,
      signal,
    });
  }
}

class FakeChild extends EventEmitter {
  constructor() {
    super();
    this.kills = [];
  }

  kill(signal) {
    this.kills.push(signal);
  }
}

test("launches with internal flags, one separator, and inherited stdio", async () => {
  // Given
  const processLike = new FakeProcess();
  const child = new FakeChild();
  const calls = [];
  const userArgs = ["run", "--", "$(touch nope)", "--flag=value"];
  const spawnImpl = (...args) => {
    calls.push(args);
    queueMicrotask(() => child.emit("exit", 0, null));
    return child;
  };

  // When
  const code = await launchAizim(assets, userArgs, processLike, spawnImpl);

  // Then
  assert.equal(code, 0);
  assert.deepEqual(calls, [
    [
      assets.launcher,
      [
        "--distribution-manifest",
        assets.distributionManifest,
        "--platform-manifest",
        assets.platformManifest,
        "--",
        ...userArgs,
      ],
      {
        env: processLike.env,
        shell: false,
        stdio: "inherit",
      },
    ],
  ]);
});

for (const [childCode, expected] of [
  [69, 69],
  [undefined, 70],
]) {
  test(`maps child exit ${childCode} to ${expected}`, async () => {
    // Given
    const processLike = new FakeProcess();
    const child = new FakeChild();
    const spawnImpl = () => {
      queueMicrotask(() => child.emit("exit", childCode, null));
      return child;
    };

    // When / Then
    assert.equal(
      await launchAizim(assets, [], processLike, spawnImpl),
      expected,
    );
  });
}

for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  test(`forwards ${signal} to the child once`, async () => {
    // Given
    const processLike = new FakeProcess();
    const child = new FakeChild();
    const launched = launchAizim(
      assets,
      [],
      processLike,
      () => child,
    );

    // When
    processLike.emit(signal);
    processLike.emit(signal);
    child.emit("exit", 0, null);

    // Then
    assert.equal(await launched, 0);
    assert.deepEqual(child.kills, [signal]);
    assert.equal(processLike.listenerCount(signal), 0);
  });
}

test("removes forwarding handlers before mirroring a child signal", async () => {
  // Given
  const processLike = new FakeProcess();
  const child = new FakeChild();
  const launched = launchAizim(assets, [], processLike, () => child);

  // When
  child.emit("exit", null, "SIGTERM");

  // Then
  assert.equal(await launched, 70);
  assert.deepEqual(processLike.kills, [
    { listeners: 0, pid: processLike.pid, signal: "SIGTERM" },
  ]);
});

test("rejects a child-process start error with a stable code", async () => {
  // Given
  const processLike = new FakeProcess();
  const child = new FakeChild();
  const launched = launchAizim(assets, [], processLike, () => {
    queueMicrotask(() => child.emit("error", new Error("secret detail")));
    return child;
  });

  // When / Then
  await assert.rejects(
    launched,
    (error) =>
      error instanceof DistributionError &&
      error.code === "LAUNCHER_UNAVAILABLE" &&
      !error.message.includes("secret detail"),
  );
});

test("prints one safe configuration error line and returns 78", () => {
  // Given
  const processLike = new FakeProcess();
  const error = new DistributionError(
    "PLATFORM_PACKAGE_MISSING",
    "compatible Aizim native package is missing",
    { cause: new Error("registry token must stay hidden") },
  );

  // When
  const code = reportDistributionError(error, processLike);

  // Then
  assert.equal(code, 78);
  assert.deepEqual(processLike.stderrLines, [
    "aizim: PLATFORM_PACKAGE_MISSING: compatible Aizim native package is missing\n",
  ]);
});
