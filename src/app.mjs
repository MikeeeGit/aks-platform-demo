import { createServer } from "node:http";

export function createApp({ version, revision, slot = "local" }) {
  if (!version || !revision || !["local", "aks01", "aks02"].includes(slot)) {
    throw new Error("Provide build version/revision and an allowed application slot.");
  }
  const identity = Object.freeze({
    application: "aks-platform-demo",
    version,
    revision,
    slot,
  });
  let ready = true;

  const server = createServer((request, response) => {
    const send = (status, value, extraHeaders = {}) => {
      const body = JSON.stringify(value) + "\n";
      response.writeHead(status, {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": Buffer.byteLength(body),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        ...extraHeaders,
      });
      response.end(request.method === "HEAD" ? undefined : body);
    };
    if (!["GET", "HEAD"].includes(request.method)) {
      send(405, { error: "method_not_allowed" }, { Allow: "GET, HEAD" });
      return;
    }
    let path;
    try {
      path = new URL(request.url, "http://localhost").pathname;
    } catch {
      send(400, { error: "invalid_request_target" });
      return;
    }
    // App Gateway can forward /api/* without rewriting the path.
    const endpoint = path.startsWith("/api/") ? path.slice(4) : path;
    switch (endpoint) {
      case "/":
        send(200, {
          ...identity,
          message: "Synthetic AKS platform delivery demo",
          endpoints: ["/healthz", "/readyz", "/version"],
        });
        break;
      case "/healthz":
        send(200, { status: "ok" });
        break;
      case "/readyz":
        send(ready ? 200 : 503, { status: ready ? "ready" : "draining" });
        break;
      case "/version":
        send(200, identity);
        break;
      default:
        send(404, { error: "not_found" });
    }
  });
  server.requestTimeout = 15_000;
  server.headersTimeout = 10_000;
  server.keepAliveTimeout = 5_000;
  server.maxRequestsPerSocket = 1000;
  return { server, setReady: (value) => { ready = Boolean(value); } };
}
