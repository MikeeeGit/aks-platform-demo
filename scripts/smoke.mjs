import http from "node:http";
import https from "node:https";
import { parseArgs } from "node:util";
import { pathToFileURL } from "node:url";

function readJson(url, host, timeoutMs) {
  return new Promise((resolve, reject) => {
    const transport = url.protocol === "https:" ? https : http;
    let timer;
    const request = transport.get(url, {
      headers: { Host: host, Accept: "application/json" },
      agent: false,
    }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
        if (Buffer.byteLength(body) > 16_384) {
          request.destroy(new Error("Smoke response exceeds 16 KiB."));
        }
      });
      response.on("error", reject);
      response.on("end", () => {
        if (response.statusCode !== 200) {
          reject(new Error("Smoke endpoint " + url.pathname + " returned HTTP " + response.statusCode + "."));
          return;
        }
        try { resolve(JSON.parse(body)); }
        catch { reject(new Error("Smoke endpoint " + url.pathname + " did not return JSON.")); }
      });
    });
    timer = setTimeout(() => request.destroy(new Error("Smoke request timed out.")), timeoutMs);
    request.on("error", reject);
    request.on("close", () => clearTimeout(timer));
  });
}

export async function smoke({ url, host, expectedSlot, expectedRevision, timeoutMs = 5000 }) {
  const base = new URL(url);
  if (!["http:", "https:"].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
    throw new Error("Provide an HTTP(S) base URL without credentials, query, or fragment.");
  }
  if (!/^[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?(?::[0-9]{1,5})?$/.test(host ?? "")) {
    throw new Error("Provide an explicit DNS Host header, optionally with a port.");
  }
  if (!["aks01", "aks02", "local"].includes(expectedSlot)) {
    throw new Error("Expected slot must be aks01, aks02, or local.");
  }
  if (!/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(expectedRevision ?? "")) {
    throw new Error("Expected revision must be the full lowercase Git commit.");
  }
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 30_000) {
    throw new Error("Timeout must be 1..30000 milliseconds per endpoint.");
  }
  const endpoint = (name) => {
    const target = new URL(base);
    target.pathname = base.pathname.replace(/\/+$/, "") + "/" + name;
    return target;
  };
  const health = await readJson(endpoint("healthz"), host, timeoutMs);
  if (health.status !== "ok") throw new Error("Liveness response does not report ok.");
  const readiness = await readJson(endpoint("readyz"), host, timeoutMs);
  if (readiness.status !== "ready") throw new Error("Readiness response does not report ready.");
  const identity = await readJson(endpoint("version"), host, timeoutMs);
  if (identity.application !== "aks-platform-demo" || identity.slot !== expectedSlot || identity.revision !== expectedRevision) {
    throw new Error("Application, slot, or full source revision does not match the selected release.");
  }
  return { result: "passed", application: identity.application, slot: identity.slot, revision: identity.revision };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const { values } = parseArgs({
      options: {
        url: { type: "string" },
        host: { type: "string" },
        "expected-slot": { type: "string" },
        "expected-revision": { type: "string" },
        "timeout-ms": { type: "string", default: "5000" },
      },
      allowPositionals: false,
    });
    const result = await smoke({
      url: values.url, host: values.host,
      expectedSlot: values["expected-slot"], expectedRevision: values["expected-revision"],
      timeoutMs: Number(values["timeout-ms"]),
    });
    console.log(JSON.stringify(result));
  } catch (error) {
    console.error("Smoke check failed: " + error.message);
    process.exitCode = 1;
  }
}
