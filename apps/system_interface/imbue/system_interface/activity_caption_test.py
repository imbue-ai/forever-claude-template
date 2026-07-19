import pytest

from imbue.system_interface.activity_caption import caption_for_tool_call


@pytest.mark.parametrize(
    "tool_name, input_preview, expected",
    [
        # --- Claude paths ---
        pytest.param("Read", '{"file_path":"/a/b/midnight.ts"}', "Reading midnight.ts", id="claude_read_basename"),
        pytest.param("Edit", '{"file_path":"src/reports.ts"}', "Editing reports.ts", id="claude_edit_basename"),
        pytest.param("Write", '{"file_path":"new.ts"}', "Writing new.ts", id="claude_write"),
        # Bash prefers the human description over the raw command.
        pytest.param(
            "Bash",
            '{"command":"git status","description":"Check working tree status"}',
            "Running Check working tree status",
            id="claude_bash_prefers_description",
        ),
        pytest.param("Bash", '{"command":"npm test"}', "Running npm test", id="claude_bash_falls_back_to_command"),
        pytest.param("Grep", '{"pattern":"registerTheme"}', 'Searching "registerTheme"', id="claude_grep_quoted"),
        pytest.param("Skill", "{}", "Loading skill…", id="claude_skill_no_target"),
        pytest.param("Skill", '{"skill":"autofix"}', "Loading skill autofix", id="claude_skill_target"),
        pytest.param(
            "WebSearch",
            '{"query":"playwright MCP setup"}',
            'Searching the web "playwright MCP setup"',
            id="claude_websearch",
        ),
        pytest.param(
            "WebFetch",
            '{"url":"https://example.com/docs"}',
            "Fetching page https://example.com/docs",
            id="claude_webfetch",
        ),
        pytest.param("Agent", '{"description":"x"}', "Delegating to sub-agent…", id="claude_agent_delegation"),
        pytest.param(
            "mcp__playwright__browser_click", "{}", "Running browser click", id="claude_mcp_parsed"
        ),
        pytest.param("some_unmapped_tool", "{}", "Running tool…", id="claude_unknown_bucket"),
    ],
)
def test_claude_caption(tool_name: str, input_preview: str, expected: str) -> None:
    assert caption_for_tool_call(tool_name, input_preview, is_codex=False) == expected


@pytest.mark.parametrize(
    "tool_name, input_preview, expected",
    [
        # code-mode exec is a JS program -> fixed label, never parsed.
        pytest.param(
            "exec",
            'const r = await tools.exec_command({cmd:"sleep 20"}); text(r.output);',
            "Running code",
            id="codex_code_mode_exec",
        ),
        pytest.param("exec_command", '{"cmd":"pytest -q"}', "Running pytest -q", id="codex_exec_command"),
        pytest.param("shell_command", '{"command":"ls -la"}', "Running ls -la", id="codex_shell_command"),
        # apply_patch: file parsed from the patch envelope header.
        pytest.param(
            "apply_patch",
            "*** Begin Patch\n*** Update File: src/app/main.py\n@@\n",
            "Editing main.py",
            id="codex_apply_patch_freeform",
        ),
        pytest.param(
            "apply_patch",
            '{"input":"*** Begin Patch\\n*** Add File: pkg/new.ts\\n"}',
            "Editing new.ts",
            id="codex_apply_patch_function_mode",
        ),
        pytest.param("view_image", '{"path":"/x/diagram.png"}', "Viewing image diagram.png", id="codex_view_image"),
        pytest.param(
            "web_search", '{"query":"beijing weather"}', 'Searching the web "beijing weather"', id="codex_web_search"
        ),
        pytest.param("mcp__srv__do_thing", "{}", "Running do thing", id="codex_mcp_parsed"),
        pytest.param("spawn_agent", "{}", "Delegating to sub-agent…", id="codex_spawn_agent_delegation"),
        # Un-verbed codex tools (update_plan, wait, write_stdin, ...) -> bucket.
        pytest.param("update_plan", '{"plan":[]}', "Running tool…", id="codex_update_plan_bucket"),
        pytest.param("wait", '{"cell_id":1}', "Running tool…", id="codex_wait_bucket"),
    ],
)
def test_codex_caption(tool_name: str, input_preview: str, expected: str) -> None:
    assert caption_for_tool_call(tool_name, input_preview, is_codex=True) == expected


def test_non_json_input_does_not_crash() -> None:
    # apply_patch freeform input is raw text, not JSON; other tools may truncate.
    # A verbed tool with no parseable target falls to "<verb>…" (like Claude's Bash).
    assert caption_for_tool_call("exec_command", "not json at all", is_codex=True) == "Running…"
    # A truly unmapped tool with no target -> the generic bucket.
    assert caption_for_tool_call("mystery_tool", "not json", is_codex=True) == "Running tool…"
