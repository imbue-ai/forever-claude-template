import { describe, expect, it } from "vitest";
import type { ToolCall } from "../models/Response";
import { codexToolLabel } from "./codexCaption";

// Codex code mode: tool_name is always "exec"; input_preview is the JS program.
function exec(js: string): ToolCall {
  return { tool_call_id: "tc1", tool_name: "exec", input_preview: js };
}

describe("codexToolLabel", () => {
  it("labels exec_command with the shell command", () => {
    expect(codexToolLabel(exec('const r = await tools.exec_command({"cmd":"sleep 3 && echo hi"});'))).toBe(
      "Running sleep 3 && echo hi",
    );
  });

  it("labels apply_patch with the file from the patch header", () => {
    expect(codexToolLabel(exec('await tools.apply_patch("*** Begin Patch\\n*** Update File: src/main.py\\n")'))).toBe(
      "Editing main.py",
    );
  });

  it("labels web_search with the query in quotes", () => {
    expect(codexToolLabel(exec('await tools.web_search({"query":"tokyo weather"})'))).toBe(
      'Searching the web "tokyo weather"',
    );
  });

  it("labels view_image with the basename", () => {
    expect(codexToolLabel(exec('await tools.view_image({"path":"/x/diagram.png"})'))).toBe(
      "Viewing image diagram.png",
    );
  });

  it("labels a known fn with no parseable target as '<verb>…'", () => {
    expect(codexToolLabel(exec("await tools.write_stdin({chars: someVar})"))).toBe("Typing into terminal…");
  });

  it("labels mcp tools in code mode by parsing the fn name", () => {
    expect(codexToolLabel(exec("await tools.mcp__srv__do_thing({})"))).toBe("Running do thing");
  });

  it("falls back to 'Running code' for an unknown tools.<fn>", () => {
    expect(codexToolLabel(exec("await tools.get_context_remaining({})"))).toBe("Running code");
  });

  it("falls back to 'Running code' when the JS has no tools.<fn> call", () => {
    expect(codexToolLabel(exec("const x = 1 + 1;"))).toBe("Running code");
  });

  it("falls back to 'Running tool…' for a non-exec tool (unexpected in code mode)", () => {
    expect(codexToolLabel({ tool_call_id: "tc1", tool_name: "shell", input_preview: "{}" })).toBe("Running tool…");
  });
});
