import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createApp } from "../src/app.mjs";
import { smoke } from "../scripts/smoke.mjs";

const revision = "c".repeat(40);
async function listen(server, t) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => { server.closeAllConnections(); server.close(); });
  return "http://127.0.0.1:" + server.address().port;
}
function options(url, overrides = {}) {
  return { url, host: "web.example.test", expectedSlot: "aks01", expectedRevision: revision, ...overrides };
}

test("smoke checks health, readiness and full source/slot metadata over HTTP", async (t) => {
  const app = createApp({ version: "0.1.0", revision, slot: "aks01" });
  const url = await listen(app.server, t);
  assert.equal((await smoke(options(url))).result, "passed");
  assert.equal((await smoke(options(url + "/api", { host: "api.example.test" }))).result, "passed");
  await assert.rejects(smoke(options(url, { expectedSlot: "aks02" })), /does not match/);
  await assert.rejects(smoke(options(url, { expectedRevision: "d".repeat(40) })), /does not match/);
  app.setReady(false);
  await assert.rejects(smoke(options(url)), /HTTP 503/);
});

test("smoke preserves explicit Host and refuses redirects or invalid metadata", async (t) => {
  let mode = "success";
  const received = [];
  const server = createServer((req, res) => {
    received.push([req.url, req.headers.host]);
    if (mode === "redirect") { res.writeHead(302, { Location: "/version" }); res.end(); return; }
    const result = req.url === "/healthz" ? { status: "ok" }
      : req.url === "/readyz" ? { status: "ready" }
      : { application: mode === "invalid" ? "other" : "aks-platform-demo", slot: "aks01", revision };
    res.end(JSON.stringify(result));
  });
  const url = await listen(server, t);
  await smoke(options(url));
  assert.deepEqual(received.map(([path]) => path), ["/healthz", "/readyz", "/version"]);
  assert.ok(received.every(([, host]) => host === "web.example.test"));
  mode = "redirect";
  await assert.rejects(smoke(options(url)), /HTTP 302/);
  mode = "invalid";
  await assert.rejects(smoke(options(url)), /does not match/);
});

test("smoke timeout is bounded and validates inputs before requesting", async (t) => {
  const server = createServer(() => {});
  const url = await listen(server, t);
  await assert.rejects(smoke(options(url, { timeoutMs: 30 })), /timed out/);
  await assert.rejects(smoke(options(url, { expectedRevision: "abc123" })), /full lowercase/);
  await assert.rejects(smoke(options(url, { url: "http://user:password@localhost" })), /without credentials/);
  await assert.rejects(smoke(options(url, { host: "bad\r\nheader" })), /explicit DNS Host/);
});

test("smoke CLI exits nonzero on selected release mismatch", async (t) => {
  const app = createApp({ version: "0.1.0", revision, slot: "aks01" });
  const url = await listen(app.server, t);
  const child = spawn(process.execPath, [
    "scripts/smoke.mjs", "--url", url, "--host", "web.example.test",
    "--expected-slot", "aks02", "--expected-revision", revision,
  ], { stdio: ["ignore", "pipe", "pipe"] });
  let stderr = "";
  child.stderr.on("data", (data) => { stderr += data; });
  const [code] = await once(child, "close");
  assert.equal(code, 1);
  assert.match(stderr, /does not match/);
});
