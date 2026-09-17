import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync, spawn } from "node:child_process";
import { once } from "node:events";
import { createServer } from "node:net";

test("build produces deterministic metadata and rejects unsafe revisions", () => {
  const env = { ...process.env, BUILD_REVISION: "b".repeat(40) };
  execFileSync(process.execPath, ["scripts/build.mjs"], { env });
  const first = readFileSync("dist/build-info.json", "utf8");
  execFileSync(process.execPath, ["scripts/build.mjs"], { env });
  assert.equal(first, readFileSync("dist/build-info.json", "utf8"));
  assert.deepEqual(JSON.parse(first), { name: "aks-platform-demo", version: JSON.parse(readFileSync("package.json", "utf8")).version, revision: "b".repeat(40) });
  assert.throws(() => execFileSync(process.execPath, ["scripts/build.mjs"], {
    env: { ...env, BUILD_REVISION: "not-a-git-revision" }, stdio: "pipe",
  }));
});

test("built process serves metadata and drains cleanly on SIGTERM", { timeout: 10_000 }, async (t) => {
  const reservation = createServer();
  reservation.listen(0, "127.0.0.1");
  await once(reservation, "listening");
  const port = reservation.address().port;
  await new Promise((resolve) => reservation.close(resolve));
  const child = spawn(process.execPath, ["dist/main.mjs"], {
    env: { ...process.env, APP_SLOT: "aks02", PORT: String(port), SHUTDOWN_DELAY_MS: "500" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  t.after(() => { if (child.exitCode === null) child.kill("SIGKILL"); });
  let output = "";
  child.stdout.on("data", (part) => { output += part; });
  const base = "http://127.0.0.1:" + port;
  const deadline = Date.now() + 5000;
  while (!output.includes('"event":"listening"')) {
    if (Date.now() > deadline) assert.fail("Built server did not become ready");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  const identity = await (await fetch(base + "/version")).json();
  assert.equal(identity.slot, "aks02");
  assert.equal(identity.revision, "b".repeat(40));
  const exited = once(child, "exit");
  child.kill("SIGTERM");
  while (!output.includes('"event":"draining"')) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal((await fetch(base + "/readyz")).status, 503);
  const [code, signal] = await exited;
  assert.equal(code, 0);
  assert.equal(signal, null);
});
