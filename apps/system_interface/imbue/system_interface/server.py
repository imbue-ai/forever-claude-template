import json
import os
import queue
import socket
import traceback
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
from flask import Flask
from flask import Response
from flask import request
from flask import send_file
from flask import send_from_directory
from flask_sock import Sock
from loguru import logger as _loguru_logger
from simple_websocket import ConnectionClosed
from werkzeug.exceptions import HTTPException

from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId
from imbue.system_interface import claude_auth_endpoints
from imbue.system_interface import latchkey_endpoints
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import discover_agents
from imbue.system_interface.agent_discovery import get_host_dir
from imbue.system_interface.agent_discovery import start_agent
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.app_context import attach_state
from imbue.system_interface.app_context import get_state
from imbue.system_interface.claude_auth import ClaudeAuthService
from imbue.system_interface.config import Config
from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.layout_ops import LayoutMutex
from imbue.system_interface.layout_ops import allocate_terminal_panel_id
from imbue.system_interface.layout_ops import is_broadcasting_op
from imbue.system_interface.layout_ops import is_known_op
from imbue.system_interface.layout_ops import is_mutating_op
from imbue.system_interface.layout_ops import layout_inspect
from imbue.system_interface.layout_ops import layout_list
from imbue.system_interface.models import AgentCreationError
from imbue.system_interface.models import AgentListItem
from imbue.system_interface.models import AgentListResponse
from imbue.system_interface.models import CreateAgentResponse
from imbue.system_interface.models import CreateChatRequest
from imbue.system_interface.models import CreateWorktreeRequest
from imbue.system_interface.models import DestroyAgentResponse
from imbue.system_interface.models import ErrorResponse
from imbue.system_interface.models import InterruptAgentResponse
from imbue.system_interface.models import RandomNameResponse
from imbue.system_interface.models import SendMessageRequest
from imbue.system_interface.models import SendMessageResponse
from imbue.system_interface.models import StartAgentResponse
from imbue.system_interface.plugins import get_plugin_manager
from imbue.system_interface.service_dispatcher import register_service_routes
from imbue.system_interface.welcome_resend import WelcomeResender
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_LOOPBACK_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

logger = _loguru_logger

STATIC_DIRECTORY = Path(__file__).parent / "static"

_FRONTEND_NOT_BUILT_HTML = (
    "<html><body><p>Frontend not built. Run <code>npm run build</code> in <code>frontend/</code>.</p></body></html>"
)

# Default number of events for tail-first loading
_DEFAULT_TAIL_COUNT = 50

# How often flask-sock sends a keepalive ping on each WebSocket connection.
# Pings detect (and tear down) half-dead peers without any asyncio machinery --
# each connection owns its own thread, so a wedged send only stalls that thread.
_WS_PING_INTERVAL_SECONDS = 25


class _ReflectClientSubprotocols:
    """A WebSocket subprotocols allow-list that accepts whatever the client offers.

    ``flask_sock`` builds one ``simple_websocket.Server`` per connection from
    ``SOCK_SERVER_OPTIONS`` and completes the WebSocket handshake (selecting and
    echoing the subprotocol) *before* our route handler runs, so a handler cannot
    choose the subprotocol per-connection. ``simple_websocket``'s default
    ``choose_subprotocol`` echoes the first client-offered subprotocol that is
    ``in`` this allow-list; making ``__contains__`` always true turns that into a
    transparent passthrough -- the server echoes back whatever subprotocol the
    client requested.

    This is required for the ``/service/<name>/`` proxy: ttyd's browser client
    opens its socket with the ``tty`` subprotocol, and Chrome aborts the
    handshake (close 1006, "press enter to reconnect" in the terminal tab) if it
    offered a subprotocol and the 101 response echoes none. Reflecting the
    offered subprotocol keeps the proxy transparent for any service, not just
    ttyd, so a new service that uses its own subprotocol works without touching
    this list.

    Clients that offer no subprotocol (the ``/api/ws`` broadcaster and the
    proto-agent-logs stream) are unaffected: with nothing offered, the
    negotiation loop never runs and no subprotocol is echoed.
    """

    def __contains__(self, _subprotocol: object) -> bool:
        return True


def _json_response(content: Any, status_code: int = 200) -> Response:
    """Build a compact JSON response, matching the wire format the frontend expects."""
    body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return Response(body, status=status_code, mimetype="application/json")


def _html_response(html_content: str, status_code: int = 200) -> Response:
    return Response(html_content, status=status_code, mimetype="text/html")


def _inject_base_path_meta_tag(html_content: str, root_path: str) -> str:
    meta_tag = f'<meta name="system-interface-base-path" content="{root_path}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


def _read_host_name() -> str:
    """Read the host name from $MNGR_HOST_DIR/data.json, falling back to socket.gethostname()."""
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if host_dir:
        data_path = Path(host_dir) / "data.json"
        if data_path.exists():
            try:
                data = json.loads(data_path.read_text())
                name = data.get("host_name")
                if name:
                    return str(name)
            except (json.JSONDecodeError, OSError):
                pass
    return socket.gethostname()


def _inject_hostname_meta_tag(html_content: str) -> str:
    hostname = _read_host_name()
    meta_tag = f'<meta name="system-interface-hostname" content="{hostname}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


def _inject_plugin_script_tags(html_content: str, plugin_basenames: list[str], root_path: str) -> str:
    script_tags = "\n".join(f'<script src="{root_path}/plugins/{basename}"></script>' for basename in plugin_basenames)
    return html_content.replace("</body>", f"{script_tags}\n</body>")


def _inject_agent_id_meta_tag(html_content: str) -> str:
    """Inject the primary agent ID as a meta tag for the frontend."""
    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    meta_tag = f'<meta name="system-interface-agent-id" content="{agent_id}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


def _index() -> Response:
    index_path = STATIC_DIRECTORY / "index.html"
    if index_path.exists():
        config: Config = get_state().config
        root_path = (request.script_root or "").rstrip("/")
        html_content = index_path.read_text()
        html_content = _inject_base_path_meta_tag(html_content, root_path)
        html_content = _inject_hostname_meta_tag(html_content)
        html_content = _inject_agent_id_meta_tag(html_content)
        if config.javascript_plugin_basenames:
            html_content = _inject_plugin_script_tags(html_content, config.javascript_plugin_basenames, root_path)
        return _html_response(html_content)
    return _html_response(_FRONTEND_NOT_BUILT_HTML)


def _index_catch_all(path: str) -> Response:
    return _index()


def _favicon() -> Response:
    favicon_path = STATIC_DIRECTORY / "favicon.ico"
    if favicon_path.exists():
        return send_file(favicon_path, mimetype="image/x-icon")
    return Response(status=404)


def _serve_asset(filename: str) -> Response:
    assets_directory = STATIC_DIRECTORY / "assets"
    return send_from_directory(assets_directory, filename)


def _discover_with_filters() -> list[AgentInfo]:
    """Discover agents using the app-level filter configuration."""
    state = get_state()
    return discover_agents(
        provider_names=state.provider_names,
        include_filters=state.include_filters,
        exclude_filters=state.exclude_filters,
    )


def _list_agents_endpoint() -> Response:
    """List all mngr-managed agents."""
    agents = _discover_with_filters()
    items = [AgentListItem(id=agent.id, name=agent.name, state=agent.state) for agent in agents]
    return _json_response(AgentListResponse(agents=items).model_dump())


def _find_agent(agent_id: str) -> AgentInfo | None:
    """Find a specific agent by ID, from the AgentManager's already-loaded state."""
    agent_manager: AgentManager = get_state().agent_manager
    return agent_manager.get_agent_info_by_id(agent_id)


def _agent_not_found_response(agent_id: str) -> Response:
    error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
    return _json_response(error.model_dump(), status_code=404)


def _get_events(agent_id: str) -> Response:
    """Get events for an agent. Supports tail-first loading and backfill."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    before_event_id = request.args.get("before")
    after_event_id = request.args.get("after")
    offset_str = request.args.get("offset")
    limit_str = request.args.get("limit", str(_DEFAULT_TAIL_COUNT))
    try:
        limit = int(limit_str)
    except ValueError:
        limit = _DEFAULT_TAIL_COUNT
    # A non-positive limit would defeat the window cap and break slicing, so fall
    # back to the default.
    if limit <= 0:
        limit = _DEFAULT_TAIL_COUNT

    watcher = get_state().get_or_create_watcher(agent_info)
    if before_event_id:
        # Page older: the `limit` events immediately before the cursor.
        events = watcher.get_backfill_events(before_event_id, limit=limit)
    elif after_event_id:
        # Page newer: the `limit` events immediately after the cursor (used when
        # the loaded window has been moved off the live tail by a jump).
        events = watcher.get_forward_events(after_event_id, limit=limit)
    elif offset_str is not None:
        # Jump: a `limit`-event window starting at an arbitrary global index, so
        # the client can land at a far scroll position in one bounded read.
        try:
            offset = int(offset_str)
        except ValueError:
            offset = 0
        events = watcher.get_events_at_offset(offset, limit)
    else:
        # Initial load: the newest `limit` events (the live tail). Bounded read
        # from the end; the client pages/jumps from here.
        events = watcher.get_tail_events(limit)

    # `total` is the full transcript length and `offset` is the global index of the
    # first returned event. Together they place the loaded window in the whole
    # conversation, so the client sizes the scrollbar for the full length and
    # derives whether more history exists above (offset > 0) and below
    # (offset + len < total) -- no separate has_more flag needed.
    total = watcher.get_total_event_count()
    offset = watcher.get_event_offset(events[0]["event_id"]) if events else total
    return _json_response({"events": events, "offset": offset, "total": total})


def _stream_filtered_events(
    agent_id: str,
    event_queues: AgentEventQueues,
    event_queue: "queue.Queue[dict[str, Any] | None]",
    should_forward: Callable[[dict[str, Any]], bool],
) -> Iterator[str]:
    """Yield SSE frames for queued events that pass ``should_forward``.

    Shared by the main agent stream and the per-subagent stream, which differ
    only in which events they keep: the main stream drops subagent-session
    events (they belong to the per-subagent stream, and would otherwise render
    the subagent's own prompt and tool calls inline in the parent thread),
    while the subagent stream keeps only its own session. Filtered-out events
    do not reset the keepalive counter. A ``None`` from the queue (shutdown
    sentinel) ends the stream.
    """
    keepalive_counter = 0
    try:
        while not event_queues.is_shutdown:
            try:
                event = event_queue.get(timeout=1)
                if event is None:
                    break
                if not should_forward(event):
                    continue
                keepalive_counter = 0
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                keepalive_counter += 1
                if keepalive_counter >= 8:
                    keepalive_counter = 0
                    yield ": keepalive\n\n"
    except GeneratorExit:
        pass
    finally:
        event_queues.unregister(agent_id, event_queue)


def _sse_response(generator: Iterator[str]) -> Response:
    return Response(
        generator,
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _stream_events(agent_id: str) -> Response:
    """SSE stream for an agent's new events."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    state = get_state()
    watcher = state.get_or_create_watcher(agent_info)

    event_queues = state.event_queues
    event_queue = event_queues.register(agent_id)

    return _sse_response(_stream_filtered_events(agent_id, event_queues, event_queue, watcher.is_main_session_event))


def _send_message_endpoint(agent_id: str) -> Response:
    """Send a message to an agent."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    send_message_request = SendMessageRequest.model_validate(request.get_json())

    agent_manager: AgentManager = get_state().agent_manager
    success = agent_manager.send_message_to_agent(AgentId(agent_info.id), send_message_request.message)
    if not success:
        error = ErrorResponse(detail=f"Failed to send message to agent '{agent_info.name}' (0 successful agents)")
        return _json_response(error.model_dump(), status_code=500)

    return _json_response(SendMessageResponse(status="ok").model_dump())


def _interrupt_agent_endpoint(agent_id: str) -> Response:
    """Interrupt an agent's current turn by restarting it.

    Runs ``mngr start <agent> --restart --no-resume``, which stops the agent
    (ending any in-progress turn) and starts it fresh without sending a resume
    message. Returns 404 if the agent is unknown, 400 if the agent carries the
    ``is_primary=true`` label, 500 if the restart command fails, 200 otherwise.

    Refuses to interrupt agents carrying the ``is_primary=true`` label: that's
    the services agent for the workspace, and restarting it would stop the
    bootstrap, telegram, web, cloudflared, and runtime-backup services. The
    frontend already hides ``is_primary=true`` agents from the visible agent
    list; this is defense-in-depth for callers that hit the endpoint directly
    (curl, scripted use, etc.).
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    if agent_info.labels.get("is_primary") == "true":
        error = ErrorResponse(
            detail=(
                f"Refusing to interrupt agent '{agent_info.name}': it carries "
                "the is_primary=true label (services agent for this workspace)"
            )
        )
        return _json_response(error.model_dump(), status_code=400)

    agent_name = agent_info.name

    result = run_local_command_modern_version(
        command=["mngr", "start", agent_name, "--restart", "--no-resume"],
        cwd=None,
        is_checked=False,
        timeout=60.0,
    )
    success = result.returncode == 0
    output = result.stdout.strip() if success else result.stderr.strip()
    if not success:
        error = ErrorResponse(detail=f"Failed to interrupt agent '{agent_name}': {output}")
        return _json_response(error.model_dump(), status_code=500)

    # The restart abandons the session transcript mid-turn, so the
    # transcript-derived activity state would stay pinned at THINKING /
    # TOOL_RUNNING until the user sends another message. Reset it to IDLE
    # now so the activity indicator clears immediately after the stop.
    get_state().agent_manager.reset_activity_state(agent_id)

    return _json_response(InterruptAgentResponse(status="ok").model_dump())


def _get_subagent_events(agent_id: str, subagent_session_id: str) -> Response:
    """Get events for a specific subagent session."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    watcher = get_state().get_or_create_watcher(agent_info)
    events = watcher.get_all_events(session_id=subagent_session_id)

    # Include metadata in the response
    metadata = watcher.get_subagent_metadata(subagent_session_id)

    return _json_response({"events": events, "metadata": metadata})


def _stream_subagent_events(agent_id: str, subagent_session_id: str) -> Response:
    """SSE stream for a subagent's new events, filtered by session_id."""
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    state = get_state()
    state.get_or_create_watcher(agent_info)

    event_queues = state.event_queues
    event_queue = event_queues.register(agent_id)

    return _sse_response(
        _stream_filtered_events(
            agent_id,
            event_queues,
            event_queue,
            lambda event: event.get("session_id") == subagent_session_id,
        )
    )


_LAYOUT_FILENAME = "layout.json"


def _primary_agent_layout_dir() -> Path | None:
    """Return the workspace layout directory for this workspace's primary agent.

    The system_interface always serves a single workspace (its own primary
    agent); the layout lives at $MNGR_HOST_DIR/agents/<MNGR_AGENT_ID>/workspace_layout/.
    Returns None if either env var is missing, which should only happen in
    dev/test setups that don't care about persistence.
    """
    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    if not agent_id:
        return None
    return get_host_dir() / "agents" / agent_id / "workspace_layout"


def _get_layout() -> Response:
    """Get the saved workspace layout for this workspace's primary agent."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return _json_response(None, status_code=404)

    layout_file = layout_dir / _LAYOUT_FILENAME
    if not layout_file.exists():
        return _json_response(None, status_code=404)

    try:
        layout_data = json.loads(layout_file.read_text())
        return _json_response(layout_data)
    except (json.JSONDecodeError, OSError):
        return _json_response(None, status_code=404)


def _save_layout() -> Response:
    """Save the workspace layout for this workspace's primary agent."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)

    body = request.get_data()
    try:
        # Validate it's valid JSON
        json.loads(body)
    except (json.JSONDecodeError, ValueError):
        error = ErrorResponse(detail="Invalid JSON in request body")
        return _json_response(error.model_dump(), status_code=400)

    layout_dir.mkdir(parents=True, exist_ok=True)
    layout_file = layout_dir / _LAYOUT_FILENAME
    layout_file.write_bytes(body)

    return _json_response({"status": "ok"})


def _get_screen_capture(agent_id: str) -> Response:
    """Capture the tmux pane content for an agent.

    Returns the visible screen content (and optionally scrollback) as plain
    text. Useful for seeing what's on an agent's terminal when it has no
    Claude session data (e.g., the agent crashed on startup).
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    prefix = os.environ.get("MNGR_PREFIX", "mngr-")
    session_name = f"{prefix}{agent_info.name}"
    include_scrollback = request.args.get("scrollback", "false").lower() == "true"
    scrollback_flag = ["-S", "-"] if include_scrollback else []
    command = ["tmux", "capture-pane", "-t", session_name, *scrollback_flag, "-p"]

    result = run_local_command_modern_version(
        command=command,
        cwd=None,
        is_checked=False,
        timeout=5.0,
    )
    success = result.returncode == 0
    if not success:
        return _json_response(
            {"screen": None, "error": f"tmux session not found: {session_name}"},
            status_code=200,
        )
    return _json_response({"screen": result.stdout})


def _serve_static_file(basename: str) -> Response:
    config: Config = get_state().config
    file_path_string = config.static_file_basename_to_path.get(basename)
    if file_path_string is None:
        error = ErrorResponse(detail=f"Static file '{basename}' not found")
        return _json_response(error.model_dump(), status_code=404)
    file_path = Path(file_path_string)
    if not file_path.is_file():
        error = ErrorResponse(detail=f"Static file not found on disk: {file_path}")
        return _json_response(error.model_dump(), status_code=404)
    return send_file(file_path)


def _random_name_endpoint() -> Response:
    """Generate a random agent name."""
    agent_manager: AgentManager = get_state().agent_manager
    name = agent_manager.generate_random_name()
    return _json_response(RandomNameResponse(name=name).model_dump())


def _create_worktree_agent() -> Response:
    """Create a new worktree agent."""
    agent_manager: AgentManager = get_state().agent_manager
    body = request.get_json()

    try:
        create_request = CreateWorktreeRequest(**body)
        agent_name = create_request.name
        selected_agent_id = create_request.selected_agent_id or agent_manager.get_own_agent_id()
        agent_id = agent_manager.create_worktree_agent(agent_name, selected_agent_id)
        return _json_response(CreateAgentResponse(agent_id=agent_id).model_dump(), status_code=201)
    except (AgentCreationError, OSError, ValueError) as e:
        error = ErrorResponse(detail=str(e))
        return _json_response(error.model_dump(), status_code=400)


def _create_chat_agent() -> Response:
    """Create a new chat agent in the primary agent's work directory."""
    agent_manager: AgentManager = get_state().agent_manager
    body = request.get_json()

    try:
        create_request = CreateChatRequest(**body)
        agent_id = agent_manager.create_chat_agent(create_request.name)
        return _json_response(CreateAgentResponse(agent_id=agent_id).model_dump(), status_code=201)
    except (AgentCreationError, OSError, ValueError) as e:
        error = ErrorResponse(detail=str(e))
        return _json_response(error.model_dump(), status_code=400)


def _ws_endpoint(websocket: Any) -> None:
    """Unified WebSocket for agent state and application updates."""
    state = get_state()
    _run_ws_broadcast_loop(
        websocket=websocket,
        agent_manager=state.agent_manager,
        ws_broadcaster=state.broadcaster,
    )


def _run_ws_broadcast_loop(
    websocket: Any,
    agent_manager: AgentManager,
    ws_broadcaster: WebSocketBroadcaster,
) -> None:
    """Stream broadcaster messages to ``websocket`` until the client disconnects.

    Each WebSocket connection owns its own thread (flask-sock + the threaded
    WSGI server), so this loop simply blocks on the per-client queue and
    forwards messages. flask-sock's ``ping_interval`` keepalive closes a
    half-dead peer, surfacing as ``ConnectionClosed`` from ``send``; the
    broadcaster can also evict a hopelessly-behind client by pushing the
    shutdown sentinel (``None``) into the queue.
    """
    client_queue = ws_broadcaster.register()
    try:
        websocket.send(
            json.dumps(
                {
                    "type": "agents_updated",
                    "agents": agent_manager.get_agents_serialized(),
                }
            )
        )
        websocket.send(
            json.dumps(
                {
                    "type": "applications_updated",
                    "applications": agent_manager.get_applications_serialized(),
                }
            )
        )

        for proto in agent_manager.get_proto_agents():
            websocket.send(json.dumps({"type": "proto_agent_created", **proto}))

        shutdown = False
        while not shutdown:
            try:
                message = client_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if message is None:
                shutdown = True
            else:
                websocket.send(message)
    except ConnectionClosed:
        pass
    finally:
        ws_broadcaster.unregister(client_queue)


def _proto_agent_logs_endpoint(websocket: Any, agent_id: str) -> None:
    """WebSocket for streaming proto-agent creation logs."""
    agent_manager: AgentManager = get_state().agent_manager
    log_queue = agent_manager.get_log_queue(agent_id)
    _run_proto_agent_logs_loop(websocket=websocket, log_queue=log_queue)


def _run_proto_agent_logs_loop(
    websocket: Any,
    log_queue: "queue.Queue[str | None] | None",
) -> None:
    """Stream ``log_queue`` messages to ``websocket`` until the proto-agent finishes.

    If ``log_queue`` is ``None`` the proto-agent does not exist; send a
    structured not-found error and close the socket.
    """
    if log_queue is None:
        try:
            websocket.send(json.dumps({"done": True, "success": False, "error": "Proto-agent not found"}))
        except ConnectionClosed:
            pass
        return

    try:
        finished = False
        while not finished:
            try:
                message = log_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if message is None:
                finished = True
            else:
                websocket.send(message)
    except ConnectionClosed:
        pass


def _build_destroy_command(agent_name: str) -> list[str]:
    """Build the ``mngr destroy --force`` argv for one agent.

    Pure: argv assembly only, so the repo<->mngr CLI contract is testable
    against the live CLI without a subprocess (see ``server_test.py``).
    """
    return ["mngr", "destroy", agent_name, "--force"]


def _destroy_agent(agent_id: str) -> Response:
    """Destroy an agent by running mngr destroy --force.

    Refuses to destroy agents carrying the ``is_primary=true`` label: that's
    the services agent for the workspace, and destroying it would tear down
    the bootstrap, telegram, web, cloudflared, and runtime-backup services
    along with it. The frontend already hides ``is_primary=true`` agents
    from the visible agent list; this is defense-in-depth for callers that
    hit the endpoint directly (curl, scripted use, etc.).
    """
    agent_manager: AgentManager = get_state().agent_manager
    agent_state = agent_manager.get_agent_by_id(agent_id)
    if agent_state is None:
        error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
        return _json_response(error.model_dump(), status_code=404)

    if agent_state.labels.get("is_primary") == "true":
        error = ErrorResponse(
            detail=(
                f"Refusing to destroy agent '{agent_state.name}': it carries "
                "the is_primary=true label (services agent for this workspace)"
            )
        )
        return _json_response(error.model_dump(), status_code=400)

    agent_name = agent_state.name

    result = run_local_command_modern_version(
        command=_build_destroy_command(agent_name),
        cwd=None,
        is_checked=False,
        timeout=30.0,
    )
    success = result.returncode == 0
    output = result.stdout.strip() if success else result.stderr.strip()
    if not success:
        error = ErrorResponse(detail=f"Failed to destroy agent '{agent_name}': {output}")
        return _json_response(error.model_dump(), status_code=500)

    # Remove the agent from the system_interface's tracked state immediately
    # so the frontend reflects the destruction without waiting for mngr observe.
    agent_manager.remove_agent(agent_id)

    return _json_response(DestroyAgentResponse(status="ok").model_dump())


def _start_agent(agent_id: str) -> Response:
    """Ensure an agent is running so its terminal session is attachable.

    Opening an agent's terminal attaches to that agent's tmux session; while
    the agent is STOPPED that session does not exist, so the attach fails
    immediately. The frontend calls this endpoint before opening a terminal
    tab -- both for the chat-page "Open agent terminal" link and for terminal
    tabs restored from a saved dockview layout.

    This goes through the exact same in-process mngr start path that sending a
    message to the agent uses (see ``agent_discovery.start_agent``), so opening
    a terminal and messaging the agent succeed or fail together rather than
    diverging. mngr's own lifecycle check makes the start a no-op for an
    already-running agent, so this is cheap in the common case.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    try:
        start_agent(agent_info.name)
    except MngrError as e:
        error = ErrorResponse(detail=f"Failed to start agent '{agent_info.name}': {e}")
        return _json_response(error.model_dump(), status_code=500)

    return _json_response(StartAgentResponse(status="ok").model_dump())


def _layout_broadcast_endpoint() -> Response:
    """Unified loopback endpoint for the agent-facing ``scripts/layout.py`` helper.

    Body: ``{op, args, agent_id}``.

    Dispatch:

    - ``list`` / ``inspect``: pure server-side queries that read the
      ``agent_manager``'s in-memory service/agent registry plus the
      persisted ``layout.json`` (for ``is_open`` flags / tree layout)
      and return a structured payload. Bypass the mutex.
    - ``refresh`` / ``reload_system_interface``: state-preserving
      broadcasts that don't mutate serialized layout. Bypass the mutex.
      ``reload_system_interface`` tells connected browsers to reload the
      whole top-level page (the frontend-reveal step of the
      ``update-system-interface`` flow).
    - All other ops (``open``, ``focus``, ``split``, ``close``, ``move``,
      ``rename``, ``maximize``, ``restore``, ``replace-url``): acquire
      the advisory mutex first; on contention return HTTP 409 with the
      holder's metadata so the caller can decide whether to retry. On
      success, broadcast the ``layout_op`` WS message and return.

    The endpoint is locked to loopback clients (no authentication exists
    between callers and the system interface inside the container).
    """
    client_host = request.remote_addr or ""
    if client_host not in _LOOPBACK_CLIENT_HOSTS:
        error = ErrorResponse(detail="layout broadcast is only callable from loopback")
        return _json_response(error.model_dump(), status_code=403)

    raw_body = request.get_data()
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError) as e:
        _loguru_logger.opt(exception=e).warning("layout broadcast received invalid JSON body")
        error = ErrorResponse(detail="Invalid JSON in request body")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(body, dict):
        error = ErrorResponse(detail="Request body must be a JSON object")
        return _json_response(error.model_dump(), status_code=400)

    op = body.get("op")
    args_raw = body.get("args", {})
    agent_id = body.get("agent_id") or request.headers.get("X-Mngr-Agent-Id") or ""
    if not isinstance(op, str) or not is_known_op(op):
        error = ErrorResponse(detail=f"Unknown layout op: {op!r}")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(args_raw, dict):
        error = ErrorResponse(detail="``args`` must be a JSON object")
        return _json_response(error.model_dump(), status_code=400)

    agent_manager: AgentManager = get_state().agent_manager
    agent_name_by_id = {a["id"]: a["name"] for a in agent_manager.get_agents_serialized()}

    if op == "list":
        layout_dir = _primary_agent_layout_dir()
        layout_path = (layout_dir / _LAYOUT_FILENAME) if layout_dir is not None else None
        entries = layout_list(
            agent_manager.list_service_names(),
            agent_manager.get_agents_serialized(),
            layout_path,
            agent_name_by_id,
        )
        # Log the caller for telemetry; v1 has no enforcement.
        logger.info("layout op={} agent_id={} entries={}", op, agent_id, len(entries))
        return _json_response({"ok": True, "entries": entries})

    if op == "inspect":
        layout_dir = _primary_agent_layout_dir()
        layout_path = (layout_dir / _LAYOUT_FILENAME) if layout_dir is not None else None
        summary = layout_inspect(layout_path, agent_name_by_id)
        logger.info("layout op={} agent_id={} panels={}", op, agent_id, len(summary.get("panels", [])))
        return _json_response({"ok": True, "layout": summary})

    if not is_broadcasting_op(op):
        # Defensive: every non-list/inspect op should broadcast. Catch
        # drift in the op-set definitions.
        error = ErrorResponse(detail=f"Op {op!r} has no broadcast handler")
        return _json_response(error.model_dump(), status_code=500)

    # Terminal creation is the one path where the script returns a ref
    # synchronously: the frontend's "New terminal" button gives each
    # terminal a freshly-minted iframe panel id, so the server pre-mints
    # one here, injects it into the broadcast args (the frontend uses it
    # verbatim), and reports the resulting ``terminal:<hash>`` ref back
    # in the HTTP response. Every other ref kind either dedups against
    # the existing panel set or is discoverable via a subsequent
    # ``inspect``.
    allocated_ref: str | None = None
    if op in {"open", "split"} and args_raw.get("ref") == "service:terminal":
        panel_id, allocated_ref = allocate_terminal_panel_id()
        args_raw = {**args_raw, "panel_id": panel_id}

    layout_mutex: LayoutMutex = get_state().layout_mutex
    broadcaster: WebSocketBroadcaster = get_state().broadcaster
    if is_mutating_op(op):
        holder = layout_mutex.try_acquire(agent_id, op, args_raw)
        if holder is not None:
            error_body = {
                "detail": (
                    f"Another layout op is in flight: agent_id={holder['agent_id']} "
                    f"op={holder['operation']}. Retry after the mutex TTL elapses."
                ),
                "retry_after_ms": layout_mutex.retry_after_ms(),
                "in_flight": holder,
            }
            return _json_response(error_body, status_code=409)
        try:
            broadcaster.broadcast_layout_op(op, args_raw, requester_agent_id=agent_id)
        finally:
            layout_mutex.release(agent_id, op)
    else:
        broadcaster.broadcast_layout_op(op, args_raw, requester_agent_id=agent_id)

    logger.info("layout op={} agent_id={} args={}", op, agent_id, args_raw)
    response_body: dict[str, Any] = {"ok": True}
    if allocated_ref is not None:
        response_body["ref"] = allocated_ref
    return _json_response(response_body)


def _handle_unhandled_exception(exc: Exception) -> Response:
    # Let werkzeug's own HTTP errors (404 routing, 405, etc.) render normally;
    # only genuine unhandled exceptions become a 500 JSON body.
    if isinstance(exc, HTTPException):
        raise exc
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    logger.error("Unhandled exception on {} {}: {}\n{}", request.method, request.path, exc, "".join(tb))
    return _json_response({"detail": f"Internal server error: {exc}"}, status_code=500)


def create_application(
    config: Config | None = None,
    provider_names: tuple[str, ...] | None = None,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
    agent_manager: AgentManager | None = None,
    claude_auth_service: ClaudeAuthService | None = None,
    welcome_resender: WelcomeResender | None = None,
    http_client: httpx.Client | None = None,
    latchkey_http_client: httpx.Client | None = None,
) -> Flask:
    # static_folder=None disables Flask's default /static route; the system
    # interface serves its own static assets explicitly below.
    application = Flask(__name__, static_folder=None)
    application.config["SOCK_SERVER_OPTIONS"] = {
        "ping_interval": _WS_PING_INTERVAL_SECONDS,
        # Echo back whatever subprotocol the client offered so the WS proxy is
        # transparent (e.g. ttyd's ``tty``); see ``_ReflectClientSubprotocols``.
        "subprotocols": _ReflectClientSubprotocols(),
    }

    # Event queues back the SSE streams; the broadcaster backs the WebSockets.
    event_queues = AgentEventQueues()

    # When a preconfigured agent manager is injected (tests), reuse it and its
    # broadcaster, and do not own its lifecycle. Otherwise build a fresh manager
    # and start the ``mngr observe`` pipeline.
    if agent_manager is None:
        broadcaster = WebSocketBroadcaster()
        resolved_agent_manager = AgentManager.build(broadcaster)
        resolved_agent_manager.start()
        is_agent_manager_owned = True
    else:
        resolved_agent_manager = agent_manager
        broadcaster = agent_manager.broadcaster
        is_agent_manager_owned = False

    # Single shared synchronous httpx client for the /service/<name>/ forwarding
    # layer; a separate one for the latchkey catalog proxy. Tests can inject
    # clients with mock transports.
    resolved_http_client = http_client or httpx.Client(follow_redirects=False, timeout=30.0)
    resolved_latchkey_http_client = latchkey_http_client or httpx.Client(timeout=30.0)

    state = SystemInterfaceState(
        config=config or Config(),
        provider_names=provider_names,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        agent_manager=resolved_agent_manager,
        broadcaster=broadcaster,
        event_queues=event_queues,
        # Advisory in-process mutex serializing layout-mutating ops. The agent
        # script never auto-retries on contention -- it surfaces the 409 to the
        # agent along with the in-flight holder's metadata.
        layout_mutex=LayoutMutex(),
        # One long-lived ClaudeAuthService per app so the in-flight OAuth
        # subprocess survives between the /start and /submit-code requests.
        claude_auth_service=claude_auth_service or ClaudeAuthService(),
        welcome_resender=welcome_resender
        or WelcomeResender(
            resolve_agent=resolved_agent_manager.get_agent_info_by_id,
            send_message_fn=resolved_agent_manager.send_message_to_agent,
        ),
        http_client=resolved_http_client,
        latchkey_http_client=resolved_latchkey_http_client,
        is_agent_manager_owned=is_agent_manager_owned,
    )
    attach_state(application, state)

    plugin_manager = get_plugin_manager()
    plugin_manager.hook.register_event_broadcaster(broadcaster=event_queues.broadcast)

    application.register_error_handler(Exception, _handle_unhandled_exception)

    plugin_manager.hook.endpoint(app=application)

    sock = Sock(application)

    application.add_url_rule("/", view_func=_index, methods=["GET"])
    application.add_url_rule("/favicon.ico", view_func=_favicon, methods=["GET"])
    application.add_url_rule("/api/agents", view_func=_list_agents_endpoint, methods=["GET"])
    application.add_url_rule("/api/agents/create-worktree", view_func=_create_worktree_agent, methods=["POST"])
    application.add_url_rule("/api/agents/create-chat", view_func=_create_chat_agent, methods=["POST"])
    application.add_url_rule("/api/random-name", view_func=_random_name_endpoint, methods=["GET"])
    application.add_url_rule("/api/agents/<agent_id>/events", view_func=_get_events, methods=["GET"])
    application.add_url_rule("/api/agents/<agent_id>/stream", view_func=_stream_events, methods=["GET"])
    application.add_url_rule("/api/agents/<agent_id>/message", view_func=_send_message_endpoint, methods=["POST"])
    application.add_url_rule("/api/agents/<agent_id>/interrupt", view_func=_interrupt_agent_endpoint, methods=["POST"])
    application.add_url_rule("/api/layout", view_func=_get_layout, methods=["GET"])
    application.add_url_rule("/api/layout", view_func=_save_layout, methods=["POST"], endpoint="_save_layout")
    application.add_url_rule("/api/agents/<agent_id>/screen", view_func=_get_screen_capture, methods=["GET"])
    application.add_url_rule("/api/agents/<agent_id>/destroy", view_func=_destroy_agent, methods=["POST"])
    application.add_url_rule("/api/agents/<agent_id>/start", view_func=_start_agent, methods=["POST"])
    claude_auth_endpoints.register_routes(application)
    latchkey_endpoints.register_routes(application)
    application.add_url_rule("/api/layout/broadcast", view_func=_layout_broadcast_endpoint, methods=["POST"])
    application.add_url_rule(
        "/api/agents/<agent_id>/subagents/<subagent_session_id>/events",
        view_func=_get_subagent_events,
        methods=["GET"],
    )
    application.add_url_rule(
        "/api/agents/<agent_id>/subagents/<subagent_session_id>/stream",
        view_func=_stream_subagent_events,
        methods=["GET"],
    )
    sock.route("/api/ws")(_ws_endpoint)
    sock.route("/api/proto-agents/<agent_id>/logs")(_proto_agent_logs_endpoint)
    application.add_url_rule("/plugins/<basename>", view_func=_serve_static_file, methods=["GET"])

    assets_directory = STATIC_DIRECTORY / "assets"
    if assets_directory.is_dir():
        application.add_url_rule("/assets/<path:filename>", view_func=_serve_asset, methods=["GET"])

    # Service forwarding routes: /service/<name>/... forwards to the service's
    # local backend (from runtime/applications.toml) with path rewriting,
    # cookie scoping, WS shim, and a scoped service worker.
    register_service_routes(application, sock)

    application.add_url_rule("/<path:path>", view_func=_index_catch_all, methods=["GET"])

    return application
