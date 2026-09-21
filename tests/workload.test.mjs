import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { mkdtemp, writeFile, rename, rm, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createApp } from "../src/app.mjs";
import { requiredFileReady } from "../src/workload.mjs";

test("ordinary profiles remain independent of a secret mount", () => {
  assert.equal(requiredFileReady(undefined)(), true);
  for (const value of ["", "relative", 3, "/bad\0path"]) {
    assert.throws(() => requiredFileReady(value), /absolute file path/);
  }
});

test("readiness fails closed on missing/empty mounts and follows rotation without exposing content", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "app-csi-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const path = join(directory, "qualification");
  const secret = "private-value-never-served";
  const app = createApp({ version: "0.1.0", revision: "a".repeat(40), slot: "aks02", requiredSecretFile: path });
  app.server.listen(0, "127.0.0.1");
  await once(app.server, "listening");
  t.after(() => new Promise(resolve => {
    app.server.close(resolve);
    app.server.closeAllConnections();
  }));
  const url = "http://127.0.0.1:" + app.server.address().port;
  const expectReady = async (status) => {
    const response = await fetch(url + "/readyz");
    assert.equal(response.status, status);
    const body = await response.text();
    assert(!body.includes(path) && !body.includes(secret));
    assert.deepEqual(JSON.parse(body), { status: status === 200 ? "ready" : "dependency_unavailable" });
    assert.equal((await fetch(url + "/healthz")).status, 200);
  };
  await expectReady(503);
  await writeFile(path, "");
  await expectReady(503);
  await writeFile(path, secret);
  await expectReady(200);
  await writeFile(join(directory, "next"), "rotated-private-value");
  await rename(join(directory, "next"), path);
  await expectReady(200);
  await rm(path);
  await expectReady(503);
  await mkdir(path);
  await expectReady(503);
  for (const endpoint of ["/", "/version", "/healthz", "/api/readyz"]) {
    const body = await (await fetch(url + endpoint)).text();
    assert(!body.includes(path) && !body.includes(secret));
  }
  app.setReady(false);
  assert.deepEqual(await (await fetch(url + "/readyz")).json(), { status: "draining" });
});
