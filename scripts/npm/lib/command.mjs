import { spawn } from "node:child_process";
import { basename } from "node:path";

function processError(program, code, signal) {
  const outcome = code === null ? `signal ${signal}` : `status ${code}`;
  return new Error(`${basename(program)} exited with ${outcome}`);
}

export async function run(
  program,
  args,
  { cwd, env = process.env, stdio = "inherit" } = {},
) {
  await new Promise((resolve, reject) => {
    const child = spawn(program, args, {
      cwd,
      env,
      stdio,
      shell: false,
    });
    child.once("error", () => {
      reject(new Error(`${basename(program)} failed to start`));
    });
    child.once("exit", (code, signal) => {
      if (code === 0) {
        resolve();
        return;
      }
      reject(processError(program, code, signal));
    });
  });
}

export async function capture(
  program,
  args,
  { cwd, env = process.env } = {},
) {
  return await new Promise((resolve, reject) => {
    const child = spawn(program, args, {
      cwd,
      env,
      shell: false,
      stdio: ["ignore", "pipe", "ignore"],
    });
    const chunks = [];
    child.stdout.on("data", (chunk) => chunks.push(chunk));
    child.once("error", () => {
      reject(new Error(`${basename(program)} failed to start`));
    });
    child.once("exit", (code, signal) => {
      if (code === 0) {
        resolve(Buffer.concat(chunks).toString("utf8").trim());
        return;
      }
      reject(processError(program, code, signal));
    });
  });
}
