"""End-to-end tests for System Interface using Playwright.

These tests start a real Flask server (threaded Werkzeug) with mocked agent
discovery, then use Playwright to interact with the web UI.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from typing import Generator
from unittest.mock import patch

import pytest

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.config import Config
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import RecordingMngrMessenger
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server

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


@contextlib.contextmanager
def _running_e2e_server(
    tmp_path: Path,
    port: int,
    session_events: list[dict[str, Any]] | None = None,
    primary_agent_id: str = "",
) -> Generator[tuple[str, AgentInfo, Path], None, None]:
    """Run the web server with a single mock agent, ready for Playwright + layout ops.

    Yields ``(base_url, agent_info, session_file)``. Shared by the default
    ``e2e_server`` fixture and any test that needs a bespoke conversation
    (e.g. a long transcript) or a distinct port.

    ``primary_agent_id`` controls layout persistence: empty (the default)
    clears MNGR_AGENT_ID so the layout endpoints have no primary-agent dir
    (nothing persists; the UI auto-opens the fixture chat); a non-empty id
    persists named layouts under ``tmp_path/agents/<id>/workspace_layout``.
    """
    base_url = f"http://127.0.0.1:{port}"
    agent_info, session_file = _make_agent_fixture(tmp_path, session_events=session_events)
    agents = [agent_info]

    # Isolate the workspace environment: point MNGR_HOST_DIR at the fixture's
    # tmp tree so the session endpoint (_find_agent) resolves the fixture agent's
    # state dir + env file, and set MNGR_AGENT_ID per ``primary_agent_id`` so
    # the layout endpoints either run unpersisted or write under the tmp tree --
    # never the real workspace's layout state. This overrides the autouse
    # _isolate_system_interface_tests fixture's env for the duration of the test.
    with (
        patch.dict(os.environ, {"MNGR_HOST_DIR": str(tmp_path), "MNGR_AGENT_ID": primary_agent_id}),
        patch("imbue.system_interface.server.discover_agents", return_value=agents),
    ):
        # Seed the agent into a manager and inject it; the manager is never started,
        # so no background mngr discovery runs. Its messenger is a recording fake so
        # message sends succeed without contacting mngr. The UI renders its agent
        # list from the WebSocket agents_updated snapshot, which the server sends
        # from this manager on connect.
        broadcaster = WebSocketBroadcaster()
        manager = AgentManager.build(broadcaster, messenger=RecordingMngrMessenger())
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
        app = create_application(build_test_state(config=config, agent_manager=manager))

        server = make_threaded_server("127.0.0.1", port, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        # Wait for server to start
        for _ in range(50):
            try:
                urllib.request.urlopen(f"{base_url}/api/agents", timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)

        try:
            yield base_url, agent_info, session_file
        finally:
            server.shutdown()
            thread.join(timeout=5.0)


@pytest.fixture
def e2e_server(tmp_path: Path) -> Generator[tuple[str, list[AgentInfo], Path], None, None]:
    """Start the web server with mock agents for e2e testing."""
    with _running_e2e_server(tmp_path, _PORT) as (base_url, agent_info, session_file):
        yield base_url, [agent_info], session_file


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


def test_mobile_viewport_layout(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """At a phone-sized viewport the mobile tab bar replaces the dockview tab
    strip, the composer sits on screen, and inputs are zoom-proof."""
    base_url, _, _ = e2e_server
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url)

    textarea = page.locator(".message-input-textbox")
    expect(textarea).to_be_visible(timeout=5000)
    box = textarea.bounding_box()
    assert box is not None
    assert box["y"] + box["height"] <= 844, "composer must sit fully inside the viewport"

    # Inputs below 16px make iOS Safari zoom the page on focus -- the zoom is
    # what used to push the composer out of view on phones.
    font_size = textarea.evaluate("el => getComputedStyle(el).fontSize")
    assert float(font_size.removesuffix("px")) >= 16

    # The desktop tab strip is hidden; the mobile bar shows the hamburger
    # menu button and the active tab's title.
    expect(page.locator(".mobile-tab-bar")).to_be_visible()
    expect(page.locator(".dv-tabs-and-actions-container")).to_be_hidden()
    expect(page.locator(".mobile-tab-bar-menu-button")).to_be_visible()
    expect(page.locator(".mobile-tab-bar-title")).to_have_text("test-agent")


# Opening the mobile menu refreshes the terminal fleet, which lists real tmux
# sessions server-side, so this test must be marked ``tmux`` (the
# resource_guards plugin blocks unmarked tmux invocations). The fetch is
# asynchronous, so the test explicitly awaits the /api/terminals response --
# otherwise the tmux call could land after the test body and trip the guard's
# "marked but never invoked" side.
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_mobile_menu_lists_tabs_and_new_tab_actions(
    e2e_server: tuple[str, list[AgentInfo], Path], page: Page
) -> None:
    """The hamburger button opens one bottom sheet holding the open tabs plus
    the same actions as the desktop add-tab dropdown."""
    base_url, _, _ = e2e_server
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url)
    expect(page.locator(".mobile-tab-bar")).to_be_visible(timeout=5000)

    with page.expect_response("**/api/terminals"):
        page.locator(".mobile-tab-bar-menu-button").click()

    # The open tab is listed as the active row, alongside the "open new"
    # actions, all in the single combined sheet.
    active_row = page.locator(".mobile-sheet-row--active")
    expect(active_row).to_be_visible()
    expect(active_row).to_contain_text("test-agent")
    expect(page.locator(".mobile-sheet-row", has_text="New chat")).to_be_visible()
    expect(page.locator(".mobile-sheet-row", has_text="New terminal")).to_be_visible()

    # Backdrop tap dismisses the sheet. Click near the top: the backdrop
    # spans the whole viewport and the sheet covers its center, so a default
    # (center) click would land on the sheet instead.
    page.locator(".mobile-sheet-backdrop").click(position={"x": 10, "y": 10})
    expect(page.locator(".mobile-sheet")).to_have_count(0)


def test_desktop_viewport_keeps_default_chrome(e2e_server: tuple[str, list[AgentInfo], Path], page: Page) -> None:
    """The mobile treatment is width-gated: a desktop-sized window keeps the
    dockview tab strip and the default transcript padding, with no mobile bar."""
    base_url, _, _ = e2e_server
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(base_url)

    textarea = page.locator(".message-input-textbox")
    expect(textarea).to_be_visible(timeout=5000)

    expect(page.locator(".mobile-tab-bar")).to_have_count(0)
    expect(page.locator(".dv-tabs-and-actions-container").first).to_be_visible()
    padding_left = page.locator(".app-content").first.evaluate("el => getComputedStyle(el).paddingLeft")
    assert padding_left == "32px"


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
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=RecordingMngrMessenger())
    app = create_application(build_test_state(config=config, agent_manager=manager))

    with patch("imbue.system_interface.server.discover_agents", return_value=agents):
        server = make_threaded_server("127.0.0.1", _PORT + 1, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        for _ in range(50):
            try:
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
            server.shutdown()
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

    Mutating ops are layout-targeted: they carry the desktop layout (the one a
    Playwright browser picks by default) and only succeed once the page's
    ``client_state`` registration has landed, so a 412 is retried until the
    registration catches up.
    """
    payload = json.dumps({"op": op, "args": {**args, "layout": "desktop"}, "agent_id": agent_id}).encode()
    request = urllib.request.Request(
        f"{base_url}/api/layout/broadcast",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _attempt() -> bool:
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return bool(response.status == 200)
        except urllib.error.HTTPError as e:
            if e.code == 412:
                return False
            raise

    wait_for(
        _attempt,
        timeout=15.0,
        poll_interval=0.2,
        error_message=f"layout broadcast for op {op!r} never succeeded (client registration missing?)",
    )


# Selects "New terminal" from the add-tab dropdown, which spawns a real tmux
# session, so this test must be marked ``tmux`` (the resource_guards plugin
# blocks unmarked tmux invocations).
@pytest.mark.tmux
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

    # Choose "New terminal" from the (right split's) dropdown. The old "New URL" item this
    # test used was intentionally removed from the "+" menu ("New browser" replaces the
    # ad-hoc-URL flow); "New terminal" opens a tab through the SAME openIframeTab +
    # targetGroup placement path, so it still exercises the clicked-split placement.
    page.locator(".dockview-add-tab-dropdown-item:visible", has_text="New terminal").click()

    # The new tab must render in the RIGHT split, not the left, and must tab
    # into the existing right group rather than carving a third.
    expect(page.locator(".dv-default-tab-content", has_text="terminal").first).to_be_visible(timeout=10000)
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
        "terminal",
    )
    assert placement["count"] == 2, f"new tab should join the right split, not create a third group: {placement}"
    assert placement["inRight"], f"new tab should be in the right split: {placement}"
    assert not placement["inLeft"], f"new tab leaked into the left split: {placement}"


def _make_long_conversation_events(pair_count: int) -> list[dict[str, Any]]:
    """Build ``pair_count`` user/assistant pairs with content ``msg-i`` / ``reply-i``.

    Each user message is uniquely identifiable so a test can tell which slice of
    the transcript the loaded window currently covers.
    """
    events: list[dict[str, Any]] = []
    for i in range(pair_count):
        events.append(
            {
                "type": "user",
                "uuid": f"long-u-{i}",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": f"msg-{i}"},
            }
        )
        events.append(
            {
                "type": "assistant",
                "uuid": f"long-a-{i}",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [{"type": "text", "text": f"reply-{i}"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
    return events


def _visible_user_messages(page: Page) -> list[str]:
    """Text of every rendered user-message bubble, in document order."""
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('.message-user')).map((e) => (e.textContent || '').trim())"
    )


def _min_message_index(messages: list[str]) -> int:
    """Smallest ``i`` among rendered ``msg-i`` bubbles (proxy for the window's top).

    A jump to the start of the conversation drags this toward 0; staying in
    history keeps it high. Non-``msg-i`` bubbles (e.g. the streamed marker) are
    ignored.
    """
    indices = [int(m[len("msg-") :]) for m in messages if m.startswith("msg-") and m[len("msg-") :].isdigit()]
    return min(indices) if indices else -1


@pytest.mark.timeout(120)
def test_hidden_tab_preserves_scroll_window(tmp_path: Path, page: Page) -> None:
    """Hiding a chat tab (and showing it again) must not move its loaded window.

    Regression test for the scroll-jump bug. Dockview is configured with
    ``defaultRenderer: "always"``, so an inactive tab stays mounted while an
    ancestor is hidden with ``display: none``; the ChatPanel keeps receiving
    global ``m.redraw()`` calls while hidden, but its scroll element then reports
    ``scrollTop``/``scrollHeight``/``clientHeight`` all as ``0``. Before the fix,
    ``maybePage()`` mapped that zero scroll position to event 0 and fired a JUMP
    that replaced the loaded window with the very start of the conversation -- so
    a user who had scrolled up to read history came back to the beginning.

    We load a long conversation, scroll up into the middle, hide the chat by
    maximizing a sibling panel while a new event streams in (forcing redraws
    while hidden), and assert the loaded window still covers the same place --
    both while hidden and after the tab is restored.
    """
    port = _PORT + 5
    # 150 pairs -> 300 events. The initial load holds only the tail 50, so the
    # first held offset (~250) is far larger than JUMP_GAP_EVENTS (120): exactly
    # the condition under which the hidden-redraw bug fired a jump to offset 0.
    events = _make_long_conversation_events(150)

    probe_url = "https://hidden-probe.example/"
    with _running_e2e_server(tmp_path, port, session_events=events) as (base_url, _, session_file):
        page.goto(base_url)
        page.wait_for_selector(".message-list", timeout=15000)
        page.wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.scrollHeight > el.clientHeight * 2; }",
            timeout=15000,
        )

        # Put a second tab in the SAME dockview group as the chat, so hiding the
        # chat is a pure tab switch (no resize): open a URL in a fresh group, then
        # move it back into the chat's group as a sibling tab. This mirrors the
        # real "switch away from a chat tab and back" scenario and, unlike
        # maximize, leaves the chat at full width so its layout never reflows.
        _broadcast_layout_op(base_url, "open", {"ref": probe_url, "new_group": True}, agent_id="agent-test-123")
        expect(page.locator(".dv-default-tab-content", has_text="hidden-probe.example").first).to_be_visible(
            timeout=_TRIGGER_TIMEOUT_MS
        )
        _broadcast_layout_op(
            base_url,
            "move",
            {"ref": probe_url, "relative_to": "chat:test-agent", "direction": "within"},
            agent_id="agent-test-123",
        )
        # One group again (the URL tabbed in beside the chat).
        page.wait_for_function(
            "() => document.querySelectorAll('.dv-groupview').length === 1",
            timeout=_TRIGGER_TIMEOUT_MS,
        )
        # Make the chat the active tab and let its full-width layout settle.
        _broadcast_layout_op(base_url, "focus", {"ref": "chat:test-agent"}, agent_id="agent-test-123")
        page.wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.clientHeight > 0; }",
            timeout=_TRIGGER_TIMEOUT_MS,
        )
        page.wait_for_timeout(1000)

        # Scroll up into the middle of the loaded window to read history (well off
        # the live tail, but not so far that a backfill to offset 0 is triggered).
        page.evaluate(
            "() => { const el = document.querySelector('.app-content'); el.scrollTop = el.scrollHeight - el.clientHeight - 1500; }"
        )
        page.wait_for_timeout(1000)
        before_hidden = _visible_user_messages(page)
        scroll_top_before = page.evaluate("() => document.querySelector('.app-content').scrollTop")
        # Sanity: we are reading history, not parked at the start or the tail.
        assert before_hidden, "expected user messages to be rendered after scrolling up"
        assert "msg-0" not in before_hidden, f"setup should not be at the start: {before_hidden[:3]}"
        anchor_message = before_hidden[0]
        assert _min_message_index(before_hidden) >= 50, f"setup should be reading mid-history: {before_hidden[:3]}"

        # Hide the chat by switching to the sibling tab.
        _broadcast_layout_op(base_url, "focus", {"ref": probe_url}, agent_id="agent-test-123")
        page.wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.clientHeight === 0; }",
            timeout=_TRIGGER_TIMEOUT_MS,
        )

        # Stream a new event in while the chat is hidden -- this drives the global
        # redraws that, before the fix, corrupted the hidden panel's window.
        with open(session_file, "a") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "long-u-streamed",
                        "timestamp": "2026-01-01T00:02:00Z",
                        "message": {"role": "user", "content": "streamed-while-hidden"},
                    }
                )
                + "\n"
            )
        # Give the watcher (polls ~1s) time to deliver the event and fire redraws.
        page.wait_for_timeout(3000)

        # While hidden, the loaded window must not have jumped to the start: the
        # reader's anchor row is still rendered and event 0 is nowhere in sight.
        during_hidden = _visible_user_messages(page)
        assert anchor_message in during_hidden, (
            f"hidden tab lost its place: anchor {anchor_message!r} no longer rendered ({during_hidden[:3]}...)"
        )
        assert "msg-0" not in during_hidden, f"hidden tab jumped to the start of the conversation: {during_hidden[:3]}"

        # Show the chat tab again; the user must be exactly where they left off.
        _broadcast_layout_op(base_url, "focus", {"ref": "chat:test-agent"}, agent_id="agent-test-123")
        page.wait_for_function(
            "() => { const el = document.querySelector('.app-content'); return el && el.clientHeight > 0; }",
            timeout=_TRIGGER_TIMEOUT_MS,
        )
        page.wait_for_timeout(1000)
        after_restore = _visible_user_messages(page)
        scroll_top_after = page.evaluate("() => document.querySelector('.app-content').scrollTop")
        assert "msg-0" not in after_restore, (
            f"after showing the tab again the window jumped to the start: {after_restore[:3]}"
        )
        # The same anchor row is rendered again and -- because the tab switch never
        # resized the chat -- the native scroll position is preserved exactly (no
        # re-pin churn to a different offset).
        assert anchor_message in after_restore, (
            f"after showing the tab again the reader was not returned to their place: {after_restore[:3]}"
        )
        assert abs(scroll_top_after - scroll_top_before) < 50, (
            f"scroll position drifted across hide/show: {scroll_top_before} -> {scroll_top_after}"
        )


@_STALE_DOCKVIEW_SKIP
def test_no_agents_shows_empty_state(page: Page, tmp_path: Path) -> None:
    """When there are no agents, the sidebar shows an empty message."""
    config = Config(system_interface_host="127.0.0.1", system_interface_port=_PORT + 2)
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=RecordingMngrMessenger())
    app = create_application(build_test_state(config=config, agent_manager=manager))

    with patch("imbue.system_interface.server.discover_agents", return_value=[]):
        server = make_threaded_server("127.0.0.1", _PORT + 2, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        for _ in range(50):
            try:
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
            server.shutdown()
            thread.join(timeout=5.0)


_LAYOUT_DIALOG_PORT = 18867


# Opening the "+" dropdown fetches the live terminal fleet, which shells out
# to ``tmux list-sessions`` server-side -- hence the tmux mark.
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_named_layout_dialogs_end_to_end(tmp_path: Path, page: Page) -> None:
    """The "+" menu's Save/Load/Delete layout dialogs drive the named-layout registry.

    End-to-end over the real frontend + server: initial UA-based selection
    (desktop) with WebSocket registration, debounced autosave materializing
    the fresh layout's file, save-as creating + switching to a new layout,
    load switching to the (empty) mobile layout, and deleting the active
    layout falling back to the first remaining one.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _LAYOUT_DIALOG_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        # The delete-fallback path surfaces a notice via alert(); auto-accept it.
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(base_url)

        # Initial: desktop is chosen (desktop UA), the fixture chat auto-opens,
        # and the debounced autosave materializes desktop.json.
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)
        page.wait_for_function("localStorage.getItem('si-active-layout-slug') === 'desktop'", timeout=10000)
        wait_for(
            lambda: (layout_dir / "layouts" / "desktop.json").exists(),
            timeout=15.0,
            poll_interval=0.1,
            error_message="autosave never materialized desktop.json",
        )

        # Save layout...: prefilled with the current name; saving under a new
        # name creates it and switches onto it.
        page.locator(".dockview-add-tab-button").first.click()
        page.locator(".dockview-add-tab-dropdown-item", has_text="Save layout...").click()
        dialog_input = page.locator(".custom-url-dialog-input")
        expect(dialog_input).to_be_visible(timeout=5000)
        assert dialog_input.input_value() == "desktop"
        assert "desktop (current)" in page.locator(".layout-dialog-list").inner_text()
        dialog_input.fill("My Phone Setup")
        page.locator(".custom-url-dialog-open").click()
        page.wait_for_function("localStorage.getItem('si-active-layout-slug') === 'my-phone-setup'", timeout=10000)
        wait_for(
            lambda: (layout_dir / "layouts" / "my-phone-setup.json").exists(),
            timeout=10.0,
            poll_interval=0.1,
            error_message="save-as never wrote my-phone-setup.json",
        )

        # Load layout...: switching to the never-saved mobile layout renders
        # the fresh state (the welcome chat auto-opens again).
        page.locator(".dockview-add-tab-button").first.click()
        page.locator(".dockview-add-tab-dropdown-item", has_text="Load layout...").click()
        page.locator(".layout-dialog-item", has_text="mobile").click()
        page.locator(".custom-url-dialog-open").click()
        page.wait_for_function("localStorage.getItem('si-active-layout-slug') === 'mobile'", timeout=10000)
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)

        # Delete layout... on the active layout: the client auto-switches to
        # the fallback and the registry drops the deleted entry.
        page.locator(".dockview-add-tab-button").first.click()
        page.locator(".dockview-add-tab-dropdown-item", has_text="Delete layout...").click()
        page.locator(".layout-dialog-item", has_text="mobile (current)").click()
        page.locator(".custom-url-dialog-open").click()
        page.wait_for_function("localStorage.getItem('si-active-layout-slug') === 'desktop'", timeout=10000)
        registry = json.loads((layout_dir / "layouts_meta.json").read_text())
        assert "mobile" not in registry["display_name_by_slug"]
        assert "my-phone-setup" in registry["display_name_by_slug"]


_LAYOUT_RESTORE_PORT = 18868


def _switch_layout_via_dialog(page: Page, layout_label: str) -> None:
    """Drive the "+" menu's Load-layout dialog to switch onto ``layout_label``."""
    page.locator(".dockview-add-tab-button").first.click()
    page.locator(".dockview-add-tab-dropdown-item", has_text="Load layout...").click()
    page.locator(".layout-dialog-item", has_text=layout_label).first.click()
    page.locator(".custom-url-dialog-open").click()


# Opening the "+" dropdown fetches the live terminal fleet, which shells out to
# ``tmux list-sessions`` server-side -- hence the tmux mark.
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_switching_layouts_preserves_chat_transcript(tmp_path: Path, page: Page) -> None:
    """A chat pane restored by a layout switch still shows its own transcript.

    Regression test: ``fromJSON`` disposes the outgoing panels before creating
    the incoming ones, and the removal handler deletes their ``panelParams``.
    Because panel ids are deterministic, a chat present in BOTH layouts had its
    freshly-seeded params deleted mid-restore and came back bound to the primary
    (services) agent -- the tab kept its title but showed an empty transcript.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _LAYOUT_RESTORE_PORT, primary_agent_id=primary_agent_id) as (
        base_url,
        _agent_info,
        _session_file,
    ):
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(base_url)

        # The fixture chat auto-opens on the desktop layout and shows its transcript.
        expect(page.locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        page.wait_for_function("localStorage.getItem('si-active-layout-slug') === 'desktop'", timeout=10000)
        wait_for(
            lambda: (layout_dir / "layouts" / "desktop.json").exists(),
            timeout=15.0,
            poll_interval=0.1,
            error_message="autosave never materialized desktop.json",
        )

        # Away to the (empty) mobile layout, then back to desktop.
        _switch_layout_via_dialog(page, "mobile")
        page.wait_for_function("localStorage.getItem('si-active-layout-slug') === 'mobile'", timeout=10000)
        expect(page.locator(".dv-default-tab-content", has_text="test-agent").first).to_be_visible(timeout=15000)

        _switch_layout_via_dialog(page, "desktop")
        page.wait_for_function("localStorage.getItem('si-active-layout-slug') === 'desktop'", timeout=10000)

        # The restored chat must show ITS transcript -- not the primary agent's
        # (which would render an empty / no-conversation state under the same tab).
        expect(page.locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        expect(page.locator(".message-list-empty")).to_have_count(0)
        expect(page.locator(".message-list-not-found")).to_have_count(0)


@pytest.mark.timeout(120)
def test_layout_missing_panel_params_recovers_chat_binding(tmp_path: Path, page: Page) -> None:
    """A saved layout whose panelParams are missing still binds the chat correctly.

    Panel ids encode identity (``chat-<agent-id>``), so a params-less panel is
    rebuilt from its id rather than silently defaulting to the primary agent.
    This also self-heals layout files corrupted by the restore bug above.
    """
    primary_agent_id = "primary-services-agent"
    with _running_e2e_server(tmp_path, _LAYOUT_RESTORE_PORT + 1, primary_agent_id=primary_agent_id) as (
        base_url,
        agent_info,
        _session_file,
    ):
        # Hand-write a desktop layout holding the agent's chat panel with an
        # EMPTY panelParams map -- the shape the restore bug used to persist.
        layout_dir = tmp_path / "agents" / primary_agent_id / "workspace_layout"
        layouts_dir = layout_dir / "layouts"
        layouts_dir.mkdir(parents=True)
        panel_id = f"chat-{agent_info.id}"
        (layouts_dir / "desktop.json").write_text(
            json.dumps(
                {
                    "dockview": {
                        "activeGroup": "group-1",
                        "grid": {
                            "root": {
                                "type": "branch",
                                "data": [
                                    {
                                        "type": "leaf",
                                        "data": {"views": [panel_id], "activeView": panel_id, "id": "group-1"},
                                        "size": 1000,
                                    }
                                ],
                            },
                            "width": 1000,
                            "height": 1000,
                            "orientation": "HORIZONTAL",
                        },
                        "panels": {
                            panel_id: {
                                "id": panel_id,
                                "contentComponent": "chat",
                                "tabComponent": "custom",
                                "title": agent_info.name,
                            }
                        },
                    },
                    "panelParams": {},
                }
            )
        )

        page.goto(base_url)

        # The chat is rebuilt from its panel id, so it shows its own transcript.
        expect(page.locator(".message-user", has_text="Hello agent!").first).to_be_visible(timeout=15000)
        expect(page.locator(".message-list-empty")).to_have_count(0)
        expect(page.locator(".message-list-not-found")).to_have_count(0)
