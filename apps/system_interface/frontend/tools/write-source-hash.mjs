#!/usr/bin/env node
// Records a deterministic hash of every git-tracked file in
// apps/system_interface/frontend/ into
// apps/system_interface/imbue/system_interface/static/.source-hash so a
// pytest ratchet (test_meta_ratchets.py::test_static_bundle_matches_frontend_source)
// can prove the committed static/ bundle is the build output of the
// committed frontend source. Pairs with the build script in
// frontend/package.json so a normal `npm run build` keeps the marker fresh.
import { createHash } from "node:crypto";
import { execSync } from "node:child_process";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontendDir = resolve(here, "..");
const staticDir = resolve(frontendDir, "../imbue/system_interface/static");

const lsOut = execSync("git ls-files", { cwd: frontendDir, encoding: "utf8" });
const files = lsOut.split("\n").filter((line) => line.length > 0).sort();

const hasher = createHash("sha256");
for (const rel of files) {
  hasher.update(rel);
  hasher.update("\0");
  hasher.update(readFileSync(join(frontendDir, rel)));
  hasher.update("\0");
}

mkdirSync(staticDir, { recursive: true });
const markerPath = join(staticDir, ".source-hash");
writeFileSync(markerPath, hasher.digest("hex") + "\n");
console.log(`[write-source-hash] ${markerPath}: ${files.length} files hashed`);
