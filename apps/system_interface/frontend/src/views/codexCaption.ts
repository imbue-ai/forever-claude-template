/**
 * Codex's TOOL_RUNNING caption. The codex peer of ``claudeCaption``; routed by the
 * agent's harness in ``ActivityIndicator``.
 *
 * Codex runs in code mode (pinned via ``features.code_mode_host``): every operation
 * is a single ``exec`` tool whose input is a JavaScript program calling
 * ``tools.<fn>({...})``. The input is arbitrary JS, so we only parse the *function
 * name* (always a literal identifier -- robust) for the verb, and make the target
 * best-effort (only when it's a plain string literal). Anything we don't recognise
 * falls back to "Running code" / "Running tool…" rather than guessing.
 */

import type { ToolCall } from "../models/Response";
import { basename, shorten } from "./captionUtils";

// tools.<fn> -> verb. `update_plan` is intentionally omitted: the codex private
// prompt forbids it, so it should never be in flight.
const VERB_BY_FN: Record<string, string> = {
  exec_command: "Running",
  shell_command: "Running",
  apply_patch: "Editing",
  web_search: "Searching the web",
  web__run: "Searching the web",
  view_image: "Viewing image",
  write_stdin: "Typing into terminal",
  read_mcp_resource: "Reading resource",
  request_user_input: "Asking",
  tool_search: "Loading tool",
};

const CODE_MODE_CALL = /tools\.([A-Za-z_]\w*)\s*\(/;
// The patch header sits in a JS string literal, so it ends at the closing quote or
// the literal ``\n`` escape between lines -- capture up to either.
const APPLY_PATCH_HEADER = /\*\*\*\s+(?:Add|Update|Delete) File:\s*([^"\\]+)/i;

// Codex emits the args as JSON (quoted keys: `"cmd":"..."`), so allow an optional
// quote around the key name.
function firstStringArg(js: string, ...keys: string[]): string | null {
  for (const key of keys) {
    const m = new RegExp(`["']?${key}["']?\\s*:\\s*"([^"]*)"`).exec(js);
    if (m) return m[1];
  }
  return null;
}

/** Best-effort literal target for a ``tools.<fn>(...)`` call. Null if not a literal. */
function targetForFn(fn: string, js: string): string | null {
  if (fn === "apply_patch") {
    const m = APPLY_PATCH_HEADER.exec(js);
    return m ? basename(m[1].trim()) : null;
  }
  if (fn === "exec_command" || fn === "shell_command") {
    const cmd = firstStringArg(js, "cmd", "command");
    return cmd !== null ? shorten(cmd) : null;
  }
  if (fn === "web_search" || fn === "web__run") {
    const q = firstStringArg(js, "q", "query");
    return q !== null ? `"${shorten(q)}"` : null;
  }
  if (fn === "view_image") {
    const path = firstStringArg(js, "path");
    return path !== null ? basename(path) : null;
  }
  return null;
}

/** "mcp__<server>__<tool>" -> "Running <tool with spaces>"; null for non-MCP names. */
function labelForMcp(fn: string): string | null {
  if (!fn.startsWith("mcp__")) return null;
  const lastSep = fn.lastIndexOf("__");
  if (lastSep <= 4) return null;
  const toolPart = fn.slice(lastSep + 2);
  if (toolPart === "") return null;
  return `Running ${toolPart.replace(/_/g, " ")}`;
}

export function codexToolLabel(tc: ToolCall): string {
  // Every codex tool in code mode is `exec`; anything else is unexpected -> generic.
  if (tc.tool_name !== "exec") return "Running tool…";
  const match = CODE_MODE_CALL.exec(tc.input_preview);
  if (match === null) return "Running code";
  const fn = match[1];

  const mcp = labelForMcp(fn);
  if (mcp !== null) return mcp;

  const verb = VERB_BY_FN[fn];
  if (verb === undefined) return "Running code";
  const target = targetForFn(fn, tc.input_preview);
  return target !== null ? `${verb} ${target}` : `${verb}…`;
}
