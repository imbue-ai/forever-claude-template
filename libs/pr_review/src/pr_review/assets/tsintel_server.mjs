// Persistent TypeScript language-service helper for pr-review's "rich types"
// mode. Driven by tsintel.py: one process per prepared repo tree.
//
// Protocol: line-delimited JSON on stdin/stdout. On startup we emit one line,
// {"ready":true} or {"ready":false,"error":...}. Then for each request line
//   {"id":N,"op":"hover"|"def","path":<repo-relative>,"line":L,"col":C}   (1-based)
// we emit one response line:
//   hover -> {"id":N,"contents":"<markdown>"}
//   def   -> {"id":N,"in_repo":bool,"path":...,"line":L,"column":C,"name":...,"type":...,"found":true}
//            or {"id":N,"found":false}
//   error -> {"id":N,"error":"..."}
// The response shapes match pr_review.jsintel so the frontend is engine-agnostic.
//
// `typescript` is loaded from the prepared tree's own node_modules (the dir the
// prepare agent verified), via createRequire rooted there. Argv:
//   node tsintel_server.mjs <treeRoot> <typescriptDir>

import { createRequire } from "module";
import path from "path";
import readline from "readline";

const treeRoot = path.resolve(process.argv[2] || ".");
const tsDir = path.resolve(process.argv[3] || treeRoot);

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

let ts;
try {
  // Resolve `typescript` from the prepared tsDir's install, not pr-review's dir.
  const requireFromTree = createRequire(path.join(tsDir, "package.json"));
  ts = requireFromTree("typescript");
} catch (e) {
  emit({ ready: false, error: "cannot load typescript: " + (e && e.message ? e.message : String(e)) });
  process.exit(1);
}

// TypeScript 7.x (the native rewrite) does not expose the classic language
// service API; only 5.x/6.x do. Fail loudly so the caller falls back rather than
// crashing on a missing enum below.
if (typeof ts.createLanguageService !== "function" || !ts.ModuleResolutionKind || !ts.ModuleKind) {
  emit({ ready: false, error: "typescript " + (ts.version || "?") + " lacks the language service API (need 5.x)" });
  process.exit(1);
}

// Pick the most lenient resolution available across TypeScript versions: we only
// read types, never emit, so Bundler/ESNext resolves node_modules types without
// import-extension strictness.
const moduleResolution =
  ts.ModuleResolutionKind.Bundler ?? ts.ModuleResolutionKind.NodeNext ?? ts.ModuleResolutionKind.NodeJs;

const compilerOptions = {
  allowJs: true,
  checkJs: false,
  module: ts.ModuleKind.ESNext,
  moduleResolution,
  target: ts.ScriptTarget.Latest,
  resolveJsonModule: true,
  allowNonTsExtensions: true,
  noEmit: true,
};

const openFiles = new Set();
const versions = new Map();

const host = {
  getScriptFileNames: () => Array.from(openFiles),
  getScriptVersion: (f) => versions.get(f) || "1",
  getScriptSnapshot: (f) => {
    const text = ts.sys.readFile(f);
    return text === undefined ? undefined : ts.ScriptSnapshot.fromString(text);
  },
  getCurrentDirectory: () => treeRoot,
  getCompilationSettings: () => compilerOptions,
  getDefaultLibFileName: (o) => ts.getDefaultLibFilePath(o),
  fileExists: ts.sys.fileExists,
  readFile: ts.sys.readFile,
  readDirectory: ts.sys.readDirectory,
  directoryExists: ts.sys.directoryExists,
  getDirectories: ts.sys.getDirectories,
  realpath: ts.sys.realpath,
};

const service = ts.createLanguageService(host, ts.createDocumentRegistry());

function ensureOpen(fileName) {
  if (!openFiles.has(fileName)) {
    if (ts.sys.readFile(fileName) === undefined) return false;
    openFiles.add(fileName);
    versions.set(fileName, "1");
  }
  return true;
}

function relInside(fileName) {
  const rel = path.relative(treeRoot, fileName);
  const inRepo = rel && !rel.startsWith("..") && !rel.split(path.sep).includes("node_modules");
  return { inRepo, rel };
}

function doHover(fileName, pos) {
  const info = service.getQuickInfoAtPosition(fileName, pos);
  if (!info) return { contents: "" };
  const sig = ts.displayPartsToString(info.displayParts || []);
  const doc = ts.displayPartsToString(info.documentation || []);
  let contents = "";
  if (sig) contents += "```typescript\n" + sig + "\n```";
  if (doc) contents += (contents ? "\n\n" : "") + doc;
  return { contents };
}

function doDef(fileName, pos) {
  const defs = service.getDefinitionAtPosition(fileName, pos);
  if (!defs || defs.length === 0) return { found: false };
  const d = defs[0];
  const program = service.getProgram();
  const sf = program && program.getSourceFile(d.fileName);
  let line = 1;
  let column = 1;
  if (sf) {
    const lc = sf.getLineAndCharacterOfPosition(d.textSpan.start);
    line = lc.line + 1;
    column = lc.character + 1;
  }
  const { inRepo, rel } = relInside(d.fileName);
  return {
    found: true,
    in_repo: !!inRepo,
    path: inRepo ? rel : d.fileName,
    line,
    column,
    name: d.name,
    type: d.kind,
  };
}

function handle(req) {
  const fileName = path.resolve(treeRoot, req.path || "");
  if (!ensureOpen(fileName)) return { id: req.id, error: "file not found" };
  const program = service.getProgram();
  const sf = program && program.getSourceFile(fileName);
  if (!sf) return { id: req.id, error: "no source file" };
  const lineCount = sf.getLineStarts().length;
  const zeroLine = (req.line || 1) - 1;
  if (zeroLine < 0 || zeroLine >= lineCount) return { id: req.id, error: "line out of range" };
  const pos = sf.getPositionOfLineAndCharacter(zeroLine, Math.max(0, (req.col || 1) - 1));
  if (req.op === "hover") return { id: req.id, ...doHover(fileName, pos) };
  if (req.op === "def") return { id: req.id, ...doDef(fileName, pos) };
  return { id: req.id, error: "unknown op" };
}

emit({ ready: true });

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let req;
  try {
    req = JSON.parse(trimmed);
  } catch {
    return;
  }
  try {
    emit(handle(req));
  } catch (e) {
    emit({ id: req.id, error: String(e && e.message ? e.message : e) });
  }
});
rl.on("close", () => process.exit(0));
