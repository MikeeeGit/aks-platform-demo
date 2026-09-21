import { mkdir, readFile, copyFile, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
const root = new URL("../", import.meta.url);
const pkg = JSON.parse(await readFile(new URL("package.json", root), "utf8"));
const revision = process.env.BUILD_REVISION || "development";
if (!/^(development|[a-f0-9]{40}|[a-f0-9]{64})$/.test(revision)) {
  throw new Error("BUILD_REVISION must be a full lowercase Git revision, or development.");
}
await mkdir(new URL("dist/", root), { recursive: true });
for (const file of ["app.mjs", "main.mjs", "workload.mjs"]) {
  execFileSync(process.execPath, ["--check", fileURLToPath(new URL("src/" + file, root))], { stdio: "inherit" });
  await copyFile(new URL("src/" + file, root), new URL("dist/" + file, root));
}
await writeFile(new URL("dist/build-info.json", root), JSON.stringify({
  name: pkg.name, version: pkg.version, revision,
}, null, 2) + "\n");
console.log("Built " + pkg.name + " " + pkg.version + " (" + revision + ")");
