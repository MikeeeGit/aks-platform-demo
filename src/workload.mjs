import { openSync, readSync, fstatSync, closeSync, constants } from "node:fs";
import { isAbsolute } from "node:path";

// CSI rotates mounts atomically. Reopen on each readiness request and never
// return or log the file path, secret content, digest or filesystem exception.
export function requiredFileReady(path) {
  if (path === undefined) return () => true;
  if (typeof path !== "string" || !isAbsolute(path) || path.includes("\0")) {
    throw new Error("APP_REQUIRED_SECRET_FILE must be an absolute file path.");
  }
  return () => {
    let descriptor;
    try {
      descriptor = openSync(path, constants.O_RDONLY | constants.O_NONBLOCK);
      if (!fstatSync(descriptor).isFile()) return false;
      const byte = Buffer.alloc(1);
      const readable = readSync(descriptor, byte, 0, 1, 0) === 1;
      byte.fill(0);
      return readable;
    } catch {
      return false;
    } finally {
      if (descriptor !== undefined) closeSync(descriptor);
    }
  };
}
