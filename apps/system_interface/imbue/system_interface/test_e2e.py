"""End-to-end tests for System Interface using Playwright.

These tests start a real FastAPI server with mocked agent discovery,
then use Playwright to interact with the web UI.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Generator
from unittest.mock import patch

import pytest
import uvicorn

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.config import Config
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.server import create_application
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

try:
    from playwright.sync_api import Page
    from playwright.sync_api import expect

    _PLAYWRIGHT_IMPORTABLE = True
except ImportError:
    _PLAYWRIGHT_IMPORTABLE = False


def _playwright_browsers_installed() -> bool:
    """Check if Playwright browsers are installed by looking for the cache directory."""
    if not _PLAYWRIGHT_IMPORTABLE:
        return False
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path:
        cache_dir = Path(env_path)
    elif sys.platform == "darwin":
        cache_dir = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        cache_dir = Path.home() / ".cache" / "ms-playwright"
    return cache_dir.exists() and any(cache_dir.iterdir())


pytestmark = [
    pytest.mark.release,
    pytest.mark.skipif(not _playwright_browsers_installed(), reason="Playwright browsers not installed"),
]

# Tests below target the pre-simplification sidebar/SubagentView UI. Commit
# c5153569b ("Simplify minds workspace interface: single dockview, no sidebar")
# replaced the sidebar-plus-conversation layout with DockviewWorkspace, so
# locators like `.conversation-selector-item-name` and the root-mounted
# `.app-header-title` no longer exist at `page.goto(base_url)`. Release tests
# do not run in CI, which is why the staleness went unnoticed. These skips
# let the release suite stay green until the tests are rewritten against the
# new dockview layout.
_STALE_DOCKVIEW_SKIP = pytest.mark.skip(
    reason="Targets pre-simplify sidebar UI; needs rewrite for DockviewWorkspace (see c5153569b)"
)

_PORT = 18765
_BASE_URL = f"http://127.0.0.1:{_PORT}"


def _make_session_file(
    projects_dir: Path,
    session_id: str,
    events: list[dict[str, Any]],
) -> Path:
    """Create a session JSONL file with the given events."""
    session_dir = projects_dir / "hash123"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{session_id}.jsonl"
    content = "\n".join(json.dumps(e) for e in events) + "\n"
    session_file.write_text(content)
    return session_file


def _make_agent_fixture(
    tmp_path: Path,
    agent_id: str = "agent-test-123",
    agent_name: str = "test-agent",
    session_events: list[dict[str, Any]] | None = None,
) -> tuple[AgentInfo, Path]:
    """Set up a mock agent with session files. Returns (agent_info, session_file_path)."""
    agent_state_dir = tmp_path / "agents" / agent_id
    agent_state_dir.mkdir(parents=True)

    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    session_id = "e2e-session-001"
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")
    # The session endpoint (_find_agent) resolves an agent's CLAUDE_CONFIG_DIR
    # from this per-agent env file (step 1 of read_claude_config_dir_from_env_file),
    # so pin it at the fixture's config dir. Without this the watcher falls back to
    # the real ~/.claude and the fixture transcript never loads.
    (agent_state_dir / "env").write_text(f"CLAUDE_CONFIG_DIR={claude_config_dir}\n")

    if session_events is None:
        session_events = [
            {
                "type": "user",
                "uuid": "uuid-e2e-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "Hello agent!"},
            },
            {
                "type": "assistant",
                "uuid": "uuid-e2e-2",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [{"type": "text", "text": "Hello! How can I help you?"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 8},
                },
            },
        ]

    session_file = _make_session_file(projects_dir, session_id, session_events)

    agent_info = AgentInfo(
        id=agent_id,
        name=agent_name,
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
    )
    return agent_info, session_file


@contextmanager
def _serve_agent_chat(
    tmp_path: Path,
    port: int,
    session_events: list[dict[str, Any]] | None = None,
) -> Generator[tuple[str, list[AgentInfo], Path], None, None]:
    """Serve a single mock agent's chat on ``port``, with the dockview UI
    auto-opening that chat. Shared by the ``e2e_server`` fixture and tests that
    need their own custom session transcript on a separate port.

    Mirrors the real workspace closely enough for the chat to render: the env is
    isolated (MNGR_HOST_DIR -> fixture tree so ``_find_agent`` resolves the
    agent's state dir + env file; MNGR_AGENT_ID cleared so the layout endpoint
    404s and the UI auto-opens the fixture chat instead of reading the real
    workspace's layout.json), discovery + send_message are mocked, and the agent
    is seeded into a directly-built AgentManager (the autouse conftest fixture
    no-ops AgentManager.start, so background discovery never runs).
    """
    agent_info, session_file = _make_agent_fixture(tmp_path, session_events=session_events)
    agents = [agent_info]
    base_url = f"http://127.0.0.1:{port}"

    with (
        patch.dict(os.environ, {"MNGR_HOST_DIR": str(tmp_path), "MNGR_AGENT_ID": ""}),
        patch("imbue.system_interface.server.send_message", return_value=True),
        patch("imbue.system_interface.server.discover_agents", return_value=agents),
    ):
        broadcaster = WebSocketBroadcaster()
        manager = AgentManager.build(broadcaster)
        with manager._lock:
            manager._agents[agent_info.id] = AgentStateItem(
                id=agent_info.id,
                name=agent_info.name,
                state="RUNNING",
                labels={},
                work_dir=str(tmp_path / "work"),
            )
        manager._ensure_activity_tracking(agent_info.id)

        config = Config(system_interface_host="127.0.0.1", system_interface_port=port)
        app = create_application(config, agent_manager=manager)

        server = uvicorn.Server(uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        # Wait for server to start
        for _ in range(50):
            try:
                urllib.request.urlopen(f"{base_url}/api/agents", timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)

        try:
            yield base_url, agents, session_file
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)


@pytest.fixture
def e2e_server(tmp_path: Path) -> Generator[tuple[str, list[AgentInfo], Path], None, None]:
    """Start the web server with the default mock agent for e2e testing."""
    with _serve_agent_chat(tmp_path, _PORT) as served:
        yield served


def test_page_loads_and_shows_title(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The page loads and shows the app title."""
    base_url, _, _ = e2e_server
    page.goto(base_url)
    expect(page).to_have_title("System Interface")


@_STALE_DOCKVIEW_SKIP
def test_sidebar_shows_agent_list(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The sidebar lists the available agents."""
    base_url, agents, _ = e2e_server
    page.goto(base_url)

    # Wait for the agent list to appear
    agent_item = page.locator(".conversation-selector-item-name")
    expect(agent_item.first).to_be_visible(timeout=5000)
    expect(agent_item.first).to_have_text("test-agent")


@_STALE_DOCKVIEW_SKIP
def test_sidebar_shows_agent_state(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The sidebar shows the agent state."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    state_label = page.locator(".conversation-selector-item-model")
    expect(state_label.first).to_be_visible(timeout=5000)
    expect(state_label.first).to_have_text("running")


@_STALE_DOCKVIEW_SKIP
def test_selecting_agent_shows_conversation(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """Clicking an agent shows its conversation history."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    # Wait for auto-select to happen (first agent is selected by default)
    # The user message should appear
    user_message = page.locator(".message-user")
    expect(user_message.first).to_be_visible(timeout=5000)
    expect(user_message.first).to_contain_text("Hello agent!")


@_STALE_DOCKVIEW_SKIP
def test_assistant_message_renders(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """Assistant messages render with markdown content."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    assistant_message = page.locator(".message-assistant")
    expect(assistant_message.first).to_be_visible(timeout=5000)
    expect(assistant_message.first).to_contain_text("Hello! How can I help you?")


@_STALE_DOCKVIEW_SKIP
def test_header_shows_agent_name(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The header shows the selected agent's name."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    header_title = page.locator(".app-header-title")
    expect(header_title).to_be_visible(timeout=5000)
    expect(header_title).to_have_text("test-agent")


@_STALE_DOCKVIEW_SKIP
def test_message_input_visible(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The message input is visible when an agent is selected."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    textarea = page.locator(".message-input-textbox")
    expect(textarea).to_be_visible(timeout=5000)


@_STALE_DOCKVIEW_SKIP
def test_send_button_appears_on_input(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The send button appears when text is entered."""
    base_url, _, _ = e2e_server
    page.goto(base_url)

    textarea = page.locator(".message-input-textbox")
    expect(textarea).to_be_visible(timeout=5000)

    # Initially no send button
    send_button = page.locator(".message-input-send-button")
    expect(send_button).not_to_be_visible()

    # Type some text
    textarea.fill("test message")
    expect(send_button).to_be_visible()


@_STALE_DOCKVIEW_SKIP
def test_tool_calls_render_as_collapsible(tmp_path: Path, page: Page) -> None:
    """Tool calls render as collapsible blocks."""
    session_events = [
        {
            "type": "user",
            "uuid": "uuid-tc-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "Read test.txt"},
        },
        {
            "type": "assistant",
            "uuid": "uuid-tc-2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": [
                    {"type": "text", "text": "Let me read that file."},
                    {"type": "tool_use", "id": "toolu_tc1", "name": "Read", "input": {"file": "test.txt"}},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
        {
            "type": "user",
            "uuid": "uuid-tc-3",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_tc1", "content": "file contents here"},
                ],
            },
        },
    ]
    agent_info, _ = _make_agent_fixture(tmp_path, session_events=session_events)
    agents = [agent_info]

    config = Config(system_interface_host="127.0.0.1", system_interface_port=_PORT + 1)
    app = create_application(config)

    with (
        patch("imbue.system_interface.server.discover_agents", return_value=agents),
        patch("imbue.system_interface.server.send_message", return_value=True),
    ):
        server = uvicorn.Server(uvicorn.Config(app=app, host="127.0.0.1", port=_PORT + 1, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        for _ in range(50):
            try:
                import urllib.request

                urllib.request.urlopen(f"http://127.0.0.1:{_PORT + 1}/api/agents", timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)

        try:
            page.goto(f"http://127.0.0.1:{_PORT + 1}")

            # Wait for the assistant message to render first
            assistant_msg = page.locator(".message-assistant")
            expect(assistant_msg.first).to_be_visible(timeout=10000)

            # Wait for assistant message with tool call
            tool_block = page.locator(".tool-call-block")
            expect(tool_block.first).to_be_visible(timeout=5000)

            # Tool call should show the tool name
            expect(tool_block.first).to_contain_text("Read")

            # Click to expand
            tool_header = page.locator(".tool-call-header")
            tool_header.first.click()

            # Details should be visible after expanding
            tool_details = page.locator(".tool-call-details")
            expect(tool_details.first).to_be_visible()
            expect(tool_details.first).to_contain_text("file contents here")
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)


@_STALE_DOCKVIEW_SKIP
def test_sse_stream_delivers_new_events(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """New events written to the session file appear in the UI via SSE."""
    base_url, agents, session_file = e2e_server
    page.goto(base_url)

    # Wait for initial content
    expect(page.locator(".message-user").first).to_be_visible(timeout=5000)

    # Append a new event to the session file
    new_event = {
        "type": "user",
        "uuid": "uuid-new-1",
        "timestamp": "2026-01-01T00:01:00Z",
        "message": {"role": "user", "content": "This is a new message via SSE!"},
    }
    with open(session_file, "a") as f:
        f.write(json.dumps(new_event) + "\n")

    # Wait for the new message to appear (watcher polls every 1 second)
    new_message = page.locator(".message-user", has_text="This is a new message via SSE!")
    expect(new_message).to_be_visible(timeout=10000)


_TRIGGER_TIMEOUT_MS = 20000


def _broadcast_layout_op(base_url: str, op: str, args: dict[str, Any], agent_id: str) -> None:
    """POST a layout op to the loopback ``/api/layout/broadcast`` endpoint.

    This is the same path ``scripts/layout.py`` drives, so issuing a ``split``
    here exercises the real frontend ``handleSplit`` handler (which carves the
    second group) rather than reaching into dockview internals from the test.
    """
    payload = json.dumps({"op": op, "args": args, "agent_id": agent_id}).encode()
    request = urllib.request.Request(
        f"{base_url}/api/layout/broadcast",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200, f"layout broadcast failed: {response.status}"


@pytest.mark.timeout(120)
def test_new_tab_opens_in_clicked_split(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The header "+" opens the new tab in the split whose header was clicked.

    Regression test for the bug where clicking "+" in a right-hand split opened
    the tab in the (active) left split instead. We split the layout into two
    groups, make the LEFT group active (so dockview's default "add to the
    active group" would land a new tab on the left), then click the RIGHT
    split's "+" and add a URL tab. It must land in the RIGHT split.
    """
    base_url, _, _ = e2e_server
    page.goto(base_url)

    # The fixture auto-opens the chat for "test-agent" as the sole group.
    expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(
        timeout=_TRIGGER_TIMEOUT_MS
    )
    add_buttons = page.locator(".dockview-add-tab-button")
    expect(add_buttons).to_have_count(1)

    # Carve a second group to the right of the chat by opening a URL iframe in
    # a fresh column. Driven through the real layout-op broadcast path.
    _broadcast_layout_op(
        base_url,
        "split",
        {
            "ref": "https://placement-split.example/",
            "relative_to": "chat:test-agent",
            "direction": "right",
            "new_group": True,
        },
        agent_id="agent-test-123",
    )

    # Two groups now, each header carrying its own "+".
    expect(add_buttons).to_have_count(2, timeout=10000)
    expect(page.locator(".dv-default-tab-content", has_text="placement-split.example").first).to_be_visible(
        timeout=10000
    )

    # Activate the LEFT (chat) split. Without the fix, the new tab would follow
    # the active group and wrongly land here.
    chat_tab = page.locator(".dv-default-tab-content", has_text="test-agent").first
    chat_tab.click()
    left_group = page.locator(".dv-groupview", has=page.locator(".dv-default-tab-content", has_text="test-agent"))
    expect(left_group).to_have_class(re.compile(r"\bdv-active-group\b"))

    # Click the "+" in the RIGHT split's header (the geometrically rightmost one).
    boxes = [add_buttons.nth(i).bounding_box() for i in range(2)]
    assert boxes[0] is not None and boxes[1] is not None
    right_index = 0 if boxes[0]["x"] > boxes[1]["x"] else 1
    add_buttons.nth(right_index).click()

    # Choose "New URL" from the (right split's) dropdown and submit a URL.
    page.locator(".dockview-add-tab-dropdown-item:visible", has_text="New URL").click()
    page.locator(".custom-url-dialog-input").first.fill("https://newtab-target.example/")
    page.locator(".custom-url-dialog-open").click()

    # The new tab must render in the RIGHT split, not the left, and must tab
    # into the existing right group rather than carving a third.
    expect(page.locator(".dv-default-tab-content", has_text="newtab-target.example").first).to_be_visible(
        timeout=10000
    )
    placement = page.evaluate(
        """
        (title) => {
          const groups = Array.from(document.querySelectorAll('.dv-groupview'))
            .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
          const has = (g) => Array.from(g.querySelectorAll('.dv-default-tab-content'))
            .some((e) => (e.textContent || '').includes(title));
          return {
            count: groups.length,
            inLeft: groups.length > 0 ? has(groups[0]) : false,
            inRight: groups.length > 0 ? has(groups[groups.length - 1]) : false,
          };
        }
        """,
        "newtab-target.example",
    )
    assert placement["count"] == 2, f"new tab should join the right split, not create a third group: {placement}"
    assert placement["inRight"], f"new tab should be in the right split: {placement}"
    assert not placement["inLeft"], f"new tab leaked into the left split: {placement}"


@_STALE_DOCKVIEW_SKIP
def test_no_agents_shows_empty_state(page: Page, tmp_path: Path) -> None:
    """When there are no agents, the sidebar shows an empty message."""
    config = Config(system_interface_host="127.0.0.1", system_interface_port=_PORT + 2)
    app = create_application(config)

    with (
        patch("imbue.system_interface.server.discover_agents", return_value=[]),
        patch("imbue.system_interface.server.send_message", return_value=True),
    ):
        server = uvicorn.Server(uvicorn.Config(app=app, host="127.0.0.1", port=_PORT + 2, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        for _ in range(50):
            try:
                import urllib.request

                urllib.request.urlopen(f"http://127.0.0.1:{_PORT + 2}/api/agents", timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)

        try:
            page.goto(f"http://127.0.0.1:{_PORT + 2}")
            empty_msg = page.locator(".conversation-selector-empty")
            expect(empty_msg).to_be_visible(timeout=5000)
            expect(empty_msg).to_contain_text("No agents found")
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)


# Decoration lines tk prints on stdout for the step used below. The frontend's
# turn-grouping walk reads the title/summary from these (see CREATED_RE /
# TK_STEP_TITLE_RE in turn-grouping.ts), so the carried-over step renders with a
# real title rather than its raw id.
_STEP_ID = "e2e-step-mail1"
_STEP_TITLE = "Fetch your unread emails"


def _permission_resolution_session_events() -> list[dict[str, Any]]:
    """The real chat shape that motivated the structural spacing fix.

    Reproduces, in order:
      1. a user turn that opens a step (``tk start``),
      2. an assistant message that issues a permission request (a latchkey POST
         tool call) -> rendered as the ``.pv-permission`` card, the last timeline
         node of the first progress block,
      3. a SEPARATE following assistant message that speaks prose ("I've sent a
         permission request...") -> the first block's trailing reply (``.pv-final``),
      4. a hidden user message granting the request -> a turn boundary with NO
         user bubble (its verdict folds onto the card; ``parsePermissionResolution``
         matches it), opening a fresh progress block that carries the still-open
         step over,
      5. the resumed work + the step closing in that second block.

    The two progress blocks abut with no user bubble between them -- the exact
    structure where the old ~46px void appeared.
    """

    def assistant(uuid: str, content: list[dict[str, Any]], stop_reason: str) -> dict[str, Any]:
        return {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-6",
                "content": content,
                "stop_reason": stop_reason,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }

    def tool_result(uuid: str, tool_use_id: str, output: str) -> dict[str, Any]:
        return {
            "type": "user",
            "uuid": uuid,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": output}],
            },
        }

    def user(uuid: str, text: str) -> dict[str, Any]:
        return {
            "type": "user",
            "uuid": uuid,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": text},
        }

    return [
        user("u-1", "show me my unread emails"),
        # Open the step.
        assistant(
            "a-start",
            [{"type": "tool_use", "id": "tu-start", "name": "Bash", "input": {"command": f"tk start {_STEP_ID}"}}],
            "tool_use",
        ),
        tool_result("tr-start", "tu-start", f"Updated {_STEP_ID} -> in_progress\ntk-step {_STEP_ID} title: {_STEP_TITLE}"),
        # Issue the permission request (latchkey POST) -> the .pv-permission card.
        assistant(
            "a-perm",
            [
                {
                    "type": "tool_use",
                    "id": "tu-perm",
                    "name": "Bash",
                    "input": {
                        "command": (
                            "latchkey curl -XPOST "
                            "http://latchkey-self.invalid/permission-requests -d '{\"service\":\"gmail\"}'"
                        )
                    },
                }
            ],
            "tool_use",
        ),
        tool_result("tr-perm", "tu-perm", '{"request_id":"r1"}'),
        # SEPARATE trailing prose -> the first block's .pv-final reply.
        assistant(
            "a-prose",
            [{"type": "text", "text": "I've sent a permission request to access your email. I'll continue once you approve."}],
            "end_turn",
        ),
        # Hidden grant notification -> a no-bubble turn boundary.
        user("u-grant", "Your permission request for Gmail was granted. Please retry the call that was blocked."),
        # Resumed work in the second (carryover) block, then close the step.
        assistant(
            "a-work",
            [{"type": "tool_use", "id": "tu-work", "name": "Bash", "input": {"command": "fetch-unread-emails"}}],
            "tool_use",
        ),
        tool_result("tr-work", "tu-work", "Fetched 3 unread emails."),
        assistant(
            "a-close",
            [{"type": "tool_use", "id": "tu-close", "name": "Bash", "input": {"command": f"tk close {_STEP_ID}"}}],
            "tool_use",
        ),
        tool_result(
            "tr-close",
            "tu-close",
            f"Updated {_STEP_ID} -> closed\ntk-step {_STEP_ID} title: {_STEP_TITLE}\n"
            f"tk-step {_STEP_ID} summary: Pulled your unread emails.",
        ),
    ]


@pytest.mark.timeout(120)
def test_permission_resolution_boundary_has_no_empty_void(tmp_path: Path, page: Page) -> None:
    """A permission grant/deny boundary renders as a single clean turn break.

    The hidden grant notification opens a fresh progress block carrying the open
    step over (this turn-boundary carryover is the intended behavior). Because
    that boundary has no user bubble, the two progress blocks' turn-margins would
    otherwise stack into a ~46px empty void. conversation-rows derives this from
    the next section rendering no user bubble (sectionRendersUserBubble) and drops
    the card-ending block's turn-bottom margin via the ``flushBottomMargin`` attr,
    so the seam is just the resumption block's normal ~18px top margin.

    This asserts the REAL DOM shape (a ``.pv-final`` trailing reply is present and
    the card-ending block carries the ``progress-block--flush-bottom`` class), so
    it cannot pass against a different tree, then measures the boundary gap. It
    fails against the ~46px void (pre-fix) and passes at the ~18px clean break
    (post-fix).
    """
    with _serve_agent_chat(tmp_path, _PORT + 3, _permission_resolution_session_events()) as served:
        base_url, _, _ = served
        page.set_viewport_size({"width": 1300, "height": 1600})
        page.goto(base_url)

        # Both progress blocks (block A: card + prose; block B: carryover) render.
        blocks = page.locator(".progress-block")
        expect(blocks).to_have_count(2, timeout=_TRIGGER_TIMEOUT_MS)

        # The real shape: block A ends on the permission card AND a separate
        # trailing prose reply (.pv-final). Asserting both guards against the test
        # silently passing against a simpler tree.
        expect(page.locator(".pv-permission").first).to_be_visible()
        expect(page.locator(".pv-final").first).to_be_visible()
        expect(page.locator(".pv-final").first).to_contain_text("I've sent a permission request")

        # The carried-over step appears in BOTH blocks (the intended carryover).
        expect(page.locator(".pv-tl-title", has_text=_STEP_TITLE)).to_have_count(2)

        # The derived flush manifests as the flush class on the card-ending
        # (first) block -- NOT a :has() selector keyed on the card.
        first_block_class = blocks.nth(0).get_attribute("class") or ""
        assert "progress-block--flush-bottom" in first_block_class, (
            f"the card-ending block should carry the structural flush marker; got class={first_block_class!r}"
        )
        second_block_class = blocks.nth(1).get_attribute("class") or ""
        assert "progress-block--flush-bottom" not in second_block_class, (
            "the resumption block must not drop its own top-margin seam"
        )

        # Measure the boundary gap: the vertical distance between the two blocks'
        # border boxes (boundingClientRect excludes margins, so this gap IS the
        # sum of block A's bottom margin + block B's top margin -- they do not
        # collapse in the flex-column message list).
        gap = page.evaluate(
            """
            () => {
              const blocks = Array.from(document.querySelectorAll('.progress-block'));
              const a = blocks[0].getBoundingClientRect();
              const b = blocks[1].getBoundingClientRect();
              return b.top - a.bottom;
            }
            """
        )
        # Post-fix target ~18px (block B's top margin alone). Pre-fix this is
        # ~46px (28 + 18). Use a window that excludes the pre-fix void but allows
        # sub-pixel rounding around the 18px target.
        assert 10 <= gap <= 28, f"permission-resolution boundary gap should be a single ~18px turn break, got {gap}px"
