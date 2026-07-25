import { DistributionError } from "./errors.mjs";

const targets = new Map([
  [
    "darwin:arm64",
    {
      id: "darwin-arm64",
      packageName: "@aiz.im/aizim-darwin-arm64",
      rustTarget: "aarch64-apple-darwin",
    },
  ],
  [
    "linux:arm64",
    {
      id: "linux-arm64",
      packageName: "@aiz.im/aizim-linux-arm64",
      rustTarget: "aarch64-unknown-linux-gnu",
    },
  ],
  [
    "linux:x64",
    {
      id: "linux-x64",
      packageName: "@aiz.im/aizim-linux-x64",
      rustTarget: "x86_64-unknown-linux-gnu",
    },
  ],
]);

export function detectTarget({
  platform = process.platform,
  arch = process.arch,
  report = platform === "linux" ? process.report.getReport() : undefined,
} = {}) {
  if (platform === "linux" && !report?.header?.glibcVersionRuntime) {
    throw new DistributionError(
      "UNSUPPORTED_PLATFORM",
      "Linux packages require glibc",
    );
  }
  const target = targets.get(`${platform}:${arch}`);
  if (!target) {
    throw new DistributionError(
      "UNSUPPORTED_PLATFORM",
      `unsupported platform ${platform}/${arch}`,
    );
  }
  return Object.freeze({ ...target });
}
