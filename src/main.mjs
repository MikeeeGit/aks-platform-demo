import { readFileSync } from "node:fs";
import { createApp } from "./app.mjs";

const build = JSON.parse(readFileSync(new URL("./build-info.json", import.meta.url), "utf8"));
const integer = (value, fallback, min, max, name) => {
  const parsed = value === undefined ? fallback : Number(value);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(name + " is outside its supported integer range.");
  }
  return parsed;
};
const port = integer(process.env.PORT, 8080, 1, 65535, "PORT");
const delay = integer(process.env.SHUTDOWN_DELAY_MS, 3000, 0, 10000, "SHUTDOWN_DELAY_MS");
const { server, setReady } = createApp({
  version: build.version,
  revision: build.revision,
  slot: process.env.APP_SLOT || "local",
});
server.listen(port, "0.0.0.0", () => {
  console.log(JSON.stringify({ event: "listening", application: build.name, port, version: build.version, revision: build.revision }));
});
server.on("error", (error) => {
  console.error(JSON.stringify({ event: "server_error", code: error.code || "unknown" }));
  process.exitCode = 1;
});
let stopping = false;
const stop = () => {
  if (stopping) return;
  stopping = true;
  setReady(false);
  console.log(JSON.stringify({ event: "draining" }));
  setTimeout(() => {
    server.close(() => { console.log(JSON.stringify({ event: "stopped" })); });
    server.closeIdleConnections();
    setTimeout(() => {
      server.closeAllConnections();
    }, 10_000).unref();
  }, delay).unref();
};
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
