import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { createApp } from "../src/app.mjs";

async function running(t, slot = "aks01") {
  const app = createApp({ version: "0.1.0", revision: "a".repeat(40), slot });
  app.server.listen(0, "127.0.0.1");
  await once(app.server, "listening");
  t.after(() => new Promise((resolve) => {
    app.server.close(resolve);
    app.server.closeAllConnections();
  }));
  return { ...app, url: "http://127.0.0.1:" + app.server.address().port };
}

test("health and readiness are independently controlled", async (t) => {
  const app = await running(t);
  assert.equal((await fetch(app.url + "/healthz")).status, 200);
  assert.equal((await fetch(app.url + "/readyz")).status, 200);
  app.setReady(false);
  const response = await fetch(app.url + "/readyz");
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { status: "draining" });
  assert.equal((await fetch(app.url + "/healthz")).status, 200);
});

test("version identifies the same immutable build and selected slot", async (t) => {
  const one = await running(t, "aks01");
  const two = await running(t, "aks02");
  const a = await (await fetch(one.url + "/version")).json();
  const b = await (await fetch(two.url + "/version")).json();
  assert.deepEqual({ ...a, slot: "aks02" }, b);
  assert.equal(a.revision, "a".repeat(40));
  assert.equal(a.version, "0.1.0");
});

test("gateway web/api Hosts and path-based API forwarding work", async (t) => {
  const app = await running(t);
  for (const host of ["web.example.test", "api.example.test", "preview.example.test"]) {
    assert.equal((await fetch(app.url + "/healthz", { headers: { Host: host } })).status, 200);
    const result = await fetch(app.url + "/api/version", { headers: { Host: host } });
    assert.equal(result.status, 200);
    assert.equal((await result.json()).slot, "aks01");
  }
});

test("HEAD, method rejection and unknown paths have deliberate responses", async (t) => {
  const app = await running(t);
  const head = await fetch(app.url + "/version", { method: "HEAD" });
  assert.equal(head.status, 200);
  assert.equal(await head.text(), "");
  const post = await fetch(app.url + "/version", { method: "POST" });
  assert.equal(post.status, 405);
  assert.equal(post.headers.get("allow"), "GET, HEAD");
  const missing = await fetch(app.url + "/not-found");
  assert.equal(missing.status, 404);
  assert.equal(missing.headers.get("cache-control"), "no-store");
  assert.equal(missing.headers.get("x-content-type-options"), "nosniff");
});

test("unrecognized slots are rejected", () => {
  assert.throws(() => createApp({ version: "0.1.0", revision: "development", slot: "unknown" }));
});
