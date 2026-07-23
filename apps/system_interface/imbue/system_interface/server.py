import json
import os
import queue
import socket
import threading
import traceback
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
from imbue.system_interface import client_activity
from imbue.system_interface import latchkey_endpoints
from imbue.system_interface import workspace_layouts
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import discover_agents
from imbue.system_interface.agent_discovery import get_host_dir
from imbue.system_interface.agent_discovery import start_agent
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.app_context import attach_state
from imbue.system_interface.app_context import get_state
from imbue.system_interface.attachments import delete_upload
from imbue.system_interface.attachments import get_uploads_directory
from imbue.system_interface.attachments import resolve_upload_path
from imbue.system_interface.attachments import store_uploaded_file
from imbue.system_interface.config import Config
from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.file_serving import try_serve_file
from imbue.system_interface.layout_ops import LayoutMutex
from imbue.system_interface.layout_ops import allocate_next_terminal_name
from imbue.system_interface.layout_ops import allocate_terminal_panel_id
from imbue.system_interface.layout_ops import filter_user_terminal_sessions
from imbue.system_interface.layout_ops import is_broadcasting_op
from imbue.system_interface.layout_ops import is_destroyable_terminal_session
from imbue.system_interface.layout_ops import is_known_op
from imbue.system_interface.layout_ops import is_mutating_op
from imbue.system_interface.layout_ops import layout_inspect
from imbue.system_interface.layout_ops import layout_list
from imbue.system_interface.layout_ops import parse_tmux_sessions_output
from imbue.system_interface.model_settings import MODEL_OPTIONS
from imbue.system_interface.model_settings import is_valid_model_id
from imbue.system_interface.model_settings import read_model_settings
from imbue.system_interface.model_settings import supports_fast_mode
from imbue.system_interface.models import ActivityRequest
from imbue.system_interface.models import ActivityResponse
from imbue.system_interface.models import AgentCreationError
from imbue.system_interface.models import AgentListItem
from imbue.system_interface.models import AgentListResponse
from imbue.system_interface.models import AttachmentError
from imbue.system_interface.models import AttachmentUploadResponse
from imbue.system_interface.models import CreateAgentResponse
from imbue.system_interface.models import CreateChatRequest
from imbue.system_interface.models import CreateWorktreeRequest
from imbue.system_interface.models import DestroyAgentResponse
from imbue.system_interface.models import ErrorResponse
from imbue.system_interface.models import InterruptAgentResponse
from imbue.system_interface.models import ModelSettingsResponse
from imbue.system_interface.models import RandomNameResponse
from imbue.system_interface.models import SendMessageRequest
from imbue.system_interface.models import SendMessageResponse
from imbue.system_interface.models import SetFastModeRequest
from imbue.system_interface.models import SetModelRequest
from imbue.system_interface.models import StartAgentResponse
from imbue.system_interface.models import TerminalSessionInfo
from imbue.system_interface.plugins import get_plugin_manager
from imbue.system_interface.service_dispatcher import register_service_routes
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
    # An agent-authored file is addressed by its absolute on-disk path, which
    # lands here as a catch-all path; serve it (image inline, any other existing
    # file as a download) before falling through to the single-page-app shell.
    # Paths that match no file are client-side routes and render the app as before.
    file_response = try_serve_file(path)
    if file_response is not None:
        return file_response
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

    # Record which client (and layout) the message came from, so agents can
    # attribute requests to a client via ``layout.py context``. Legacy callers
    # without client metadata (curl, older frontends) are simply not recorded.
    events_path = _client_activity_events_path()
    if events_path is not None and send_message_request.client_id:
        client_activity.append_message_event(
            events_path,
            client_id=send_message_request.client_id,
            device_kind=send_message_request.device_kind,
            layout_slug=send_message_request.active_layout,
            agent_id=agent_info.id,
            agent_name=agent_info.name,
            message_text=send_message_request.message,
        )

    return _json_response(SendMessageResponse(status="ok").model_dump())


def _get_model_settings_endpoint(agent_id: str) -> Response:
    """Return the agent's current model + fast-mode selection for the composer picker.

    Reads the agent's Claude Code ``settings.json`` (the source of truth the
    ``/model`` and ``/fast`` commands write to). ``fast_mode_supported`` reflects
    the current model so the frontend knows whether to surface the fast toggle.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    settings_path = agent_info.claude_config_dir / "settings.json"
    model, fast_mode = read_model_settings(settings_path)
    response = ModelSettingsResponse(
        model=model,
        fast_mode=fast_mode,
        fast_mode_supported=supports_fast_mode(model),
        options=MODEL_OPTIONS,
    )
    return _json_response(response.model_dump())


def _set_model_endpoint(agent_id: str) -> Response:
    """Switch the agent's Claude Code model by sending it a ``/model <id>`` command.

    Delivered through the same interactive-send path as a chat message, so the
    running session applies the change immediately and Claude Code persists it as
    the agent's default (its ``settings.json`` ``model`` field). Returns 400 for
    an unknown model id, 404 for an unknown agent, 500 if the command could not be
    delivered.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    set_model_request = SetModelRequest.model_validate(request.get_json())
    if not is_valid_model_id(set_model_request.model):
        error = ErrorResponse(detail=f"Unknown model '{set_model_request.model}'")
        return _json_response(error.model_dump(), status_code=400)

    agent_manager: AgentManager = get_state().agent_manager
    success = agent_manager.send_message_to_agent(AgentId(agent_info.id), f"/model {set_model_request.model}")
    if not success:
        error = ErrorResponse(detail=f"Failed to switch model for agent '{agent_info.name}' (0 successful agents)")
        return _json_response(error.model_dump(), status_code=500)

    return _json_response(SendMessageResponse(status="ok").model_dump())


def _set_fast_mode_endpoint(agent_id: str) -> Response:
    """Toggle the agent's fast mode by sending it a ``/fast on|off`` command.

    Same interactive-send path as ``_set_model_endpoint``; Claude Code persists
    the choice to its ``settings.json`` ``fastMode`` field. Fast mode is an
    Opus-only capability, so the frontend only surfaces the toggle for Opus; this
    endpoint does not re-check the model, matching how ``/fast`` itself behaves.
    """
    agent_info = _find_agent(agent_id)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    set_fast_mode_request = SetFastModeRequest.model_validate(request.get_json())
    command = "/fast on" if set_fast_mode_request.enabled else "/fast off"

    agent_manager: AgentManager = get_state().agent_manager
    success = agent_manager.send_message_to_agent(AgentId(agent_info.id), command)
    if not success:
        error = ErrorResponse(detail=f"Failed to set fast mode for agent '{agent_info.name}' (0 successful agents)")
        return _json_response(error.model_dump(), status_code=500)

    return _json_response(SendMessageResponse(status="ok").model_dump())


def _activity_endpoint() -> Response:
    """Report the workspace UI's current agent-tab activity for OOM prioritization.

    The frontend posts a snapshot ({open, visible, messaged}) whenever a tab
    opens/closes, the visible tab changes, or a message is sent. The agent manager
    hands it to the chat OOM prioritizer, which re-tags each chat agent's
    ``oom_score_adj`` so more-engaged chats are more protected from a memory shed
    (workers and the primary agent are excluded and never re-tagged). Best-effort
    and idempotent: the endpoint just records the snapshot and returns ok.
    """
    activity_request = ActivityRequest.model_validate(request.get_json())
    agent_manager: AgentManager = get_state().agent_manager
    agent_manager.record_activity(
        open_ids=activity_request.open,
        visible_ids=activity_request.visible,
        messaged_id=activity_request.messaged,
    )
    return _json_response(ActivityResponse(status="ok").model_dump())


def _upload_attachment() -> Response:
    """Store a file the user attached to a chat message under uploads/.

    The frontend uploads each attachment here as soon as the user drops, pastes,
    or picks it, then appends the returned absolute path to the message text it
    sends to the agent. Returns the stored path and size so the composer can show
    a preview and reference the file.
    """
    file_storage = request.files.get("file")
    if file_storage is None or not file_storage.filename:
        error = ErrorResponse(detail="No file provided in the 'file' field")
        return _json_response(error.model_dump(), status_code=400)

    uploads_directory = get_uploads_directory()
    try:
        stored_path = store_uploaded_file(uploads_directory, file_storage.filename, file_storage)
    except AttachmentError as e:
        error = ErrorResponse(detail=str(e))
        return _json_response(error.model_dump(), status_code=500)

    size_bytes = stored_path.stat().st_size
    response = AttachmentUploadResponse(path=str(stored_path), size=size_bytes)
    return _json_response(response.model_dump(), status_code=201)


def _serve_attachment(relative_path: str) -> Response:
    """Serve a stored attachment for inline preview, confined to uploads/."""
    resolved_path = resolve_upload_path(get_uploads_directory(), relative_path)
    if resolved_path is None:
        error = ErrorResponse(detail=f"Attachment '{relative_path}' not found")
        return _json_response(error.model_dump(), status_code=404)
    return send_file(resolved_path)


def _delete_attachment(relative_path: str) -> Response:
    """Delete a stored attachment when the user removes it before sending.

    Idempotent: a path that is missing or escapes the uploads directory is a
    no-op, so a double-remove or a stale id still reports success.
    """
    delete_upload(get_uploads_directory(), relative_path)
    return _json_response({"status": "ok"})


def _interrupt_agent_endpoint(agent_id: str) -> Response:
    """Interrupt an agent's current turn by restarting it.

    Runs ``mngr start <agent> --restart --no-resume``, which stops the agent
    (ending any in-progress turn) and starts it fresh without sending a resume
    message. Returns 404 if the agent is unknown, 400 if the agent carries the
    ``is_primary=true`` label, 500 if the restart command fails, 200 otherwise.

    Refuses to interrupt agents carrying the ``is_primary=true`` label: that's
    the services agent for the workspace, and restarting it would stop the
    bootstrap, web, cloudflared, and other supervised services. The
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


# Stores the user's "never show again" choice for the terminal lifecycle banner,
# alongside the named layouts in the primary agent's workspace_layout dir.
_TERMINAL_BANNER_FILENAME = "terminal_banner.json"

# Serializes terminal-name allocation and tracks names handed out but not yet
# materialized as live tmux sessions (session creation is lazy, on ttyd connect).
_terminal_allocate_lock = threading.Lock()
_recently_allocated_terminal_names: set[str] = set()


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


def _client_activity_events_path() -> Path | None:
    """Where the workspace-level client-activity event log lives, or None."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return None
    return client_activity.get_events_path(layout_dir)


def _parse_json_object_body() -> dict[str, Any] | Response:
    """Parse the request body as a JSON object, or return a 400 error response."""
    try:
        body = json.loads(request.get_data())
    except (json.JSONDecodeError, ValueError) as e:
        _loguru_logger.opt(exception=e).warning("Request to {} carried invalid JSON", request.path)
        error = ErrorResponse(detail="Invalid JSON in request body")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(body, dict):
        error = ErrorResponse(detail="Request body must be a JSON object")
        return _json_response(error.model_dump(), status_code=400)
    return body


def _default_layout_infos() -> list[dict[str, Any]]:
    """The two default layout names, for dev/test setups with no layout dir."""
    return [
        workspace_layouts.LayoutInfo(slug=slug, display_name=slug, has_content=False).model_dump()
        for slug in (workspace_layouts.DESKTOP_LAYOUT_SLUG, workspace_layouts.MOBILE_LAYOUT_SLUG)
    ]


def _list_layouts_endpoint() -> Response:
    """List every named layout plus the last-active slug."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        # No primary agent configured (dev/test): expose the default names so
        # the frontend can still pick an active layout; nothing persists.
        return _json_response(
            {"layouts": _default_layout_infos(), "last_active_slug": workspace_layouts.DESKTOP_LAYOUT_SLUG}
        )
    infos = workspace_layouts.list_layouts(layout_dir)
    return _json_response(
        {
            "layouts": [info.model_dump() for info in infos],
            "last_active_slug": workspace_layouts.get_last_active_slug(layout_dir),
        }
    )


def _get_named_layout_endpoint(slug: str) -> Response:
    """Get one named layout's saved content (null when the layout is still empty)."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return _json_response({"slug": slug, "display_name": slug, "layout": None})
    try:
        content = workspace_layouts.read_layout_content(layout_dir, slug)
        display_name = workspace_layouts.get_layout_display_name(layout_dir, slug)
    except workspace_layouts.LayoutNotFoundError:
        error = ErrorResponse(detail=f"Layout '{slug}' not found")
        return _json_response(error.model_dump(), status_code=404)
    return _json_response({"slug": slug, "display_name": display_name, "layout": content})


def _save_layout_as_endpoint() -> Response:
    """Save the posted layout under a display name (creating or overwriting).

    The server owns slugification: an exact display-name match overwrites
    that layout, while a slug collision with a *different* display name is
    rejected so two visually-distinct names never share a file.
    """
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    body = _parse_json_object_body()
    if isinstance(body, Response):
        return body
    display_name = body.get("display_name")
    layout_content = body.get("layout")
    client_id = str(body.get("client_id") or "")
    if not isinstance(display_name, str) or not display_name.strip():
        error = ErrorResponse(detail="'display_name' must be a non-empty string")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(layout_content, dict):
        error = ErrorResponse(detail="'layout' must be a JSON object")
        return _json_response(error.model_dump(), status_code=400)
    try:
        slug = workspace_layouts.register_layout(layout_dir, display_name.strip())
    except workspace_layouts.LayoutNameError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=400)
    except workspace_layouts.LayoutConflictError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=409)
    workspace_layouts.write_layout_content(layout_dir, slug, layout_content)
    resolved_display_name = workspace_layouts.get_layout_display_name(layout_dir, slug)
    get_state().broadcaster.broadcast_layout_saved(slug, resolved_display_name, client_id)
    return _json_response({"slug": slug, "display_name": resolved_display_name})


def _autosave_named_layout_endpoint(slug: str) -> Response:
    """Persist the posted content to an existing named layout (the autosave path)."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    body = _parse_json_object_body()
    if isinstance(body, Response):
        return body
    layout_content = body.get("layout")
    client_id = str(body.get("client_id") or "")
    if not isinstance(layout_content, dict):
        error = ErrorResponse(detail="'layout' must be a JSON object")
        return _json_response(error.model_dump(), status_code=400)
    try:
        workspace_layouts.write_layout_content(layout_dir, slug, layout_content)
        display_name = workspace_layouts.get_layout_display_name(layout_dir, slug)
    except workspace_layouts.LayoutNotFoundError:
        # The layout was deleted while this client's autosave was in flight;
        # the client hears about the deletion over the WebSocket.
        error = ErrorResponse(detail=f"Layout '{slug}' not found")
        return _json_response(error.model_dump(), status_code=404)
    get_state().broadcaster.broadcast_layout_saved(slug, display_name, client_id)
    return _json_response({"status": "ok"})


def _delete_named_layout_endpoint(slug: str) -> Response:
    """Delete a named layout; the last remaining layout cannot be deleted."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    try:
        fallback_slug = workspace_layouts.delete_layout(layout_dir, slug)
    except workspace_layouts.LayoutNotFoundError:
        error = ErrorResponse(detail=f"Layout '{slug}' not found")
        return _json_response(error.model_dump(), status_code=404)
    except workspace_layouts.LastLayoutDeletionError as e:
        return _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=409)
    get_state().broadcaster.broadcast_layout_deleted(slug, fallback_slug)
    return _json_response({"status": "ok", "fallback_layout_slug": fallback_slug})


def _tmux_prefix() -> str:
    """The mngr session-name prefix; agent sessions carry it, terminals do not."""
    return os.environ.get("MNGR_PREFIX", "mngr-")


def _list_tmux_sessions() -> tuple[TerminalSessionInfo, ...]:
    """Enumerate every tmux session on the default socket, or () when none.

    A missing tmux server (no sessions yet) returns a non-zero exit code, which
    we treat as an empty list rather than an error.
    """
    result = run_local_command_modern_version(
        command=["tmux", "list-sessions", "-F", "#{session_name}\t#{session_id}\t#{session_path}"],
        cwd=None,
        is_checked=False,
        timeout=5.0,
    )
    if result.returncode != 0:
        return ()
    return parse_tmux_sessions_output(result.stdout)


def _list_terminals() -> Response:
    """List the live user-terminal tmux sessions (excludes mngr agent sessions)."""
    prefix = _tmux_prefix()
    sessions = filter_user_terminal_sessions(_list_tmux_sessions(), prefix)
    return _json_response(
        {
            "terminals": [session.model_dump() for session in sessions],
            "prefix": prefix,
        }
    )


def _allocate_terminal() -> Response:
    """Reserve the next free ``terminal-<N>`` name for a new terminal tab.

    The lock plus the in-memory ``_recently_allocated_terminal_names`` set make
    consecutive allocations return distinct names even before the ttyd
    connection has actually created the tmux session (creation is lazy, so two
    rapid clicks would otherwise both see the same live-session set and collide).
    """
    prefix = _tmux_prefix()
    with _terminal_allocate_lock:
        live_names = {session.session_name for session in _list_tmux_sessions()}
        # Drop reservations that have since become real sessions so the set
        # cannot grow without bound.
        _recently_allocated_terminal_names.difference_update(live_names)
        taken = live_names | _recently_allocated_terminal_names
        name = allocate_next_terminal_name(taken, prefix)
        _recently_allocated_terminal_names.add(name)
    return _json_response({"session_name": name})


def _destroy_terminal(session_name: str) -> Response:
    """Kill a user-terminal tmux session. Refuses to touch mngr agent sessions."""
    prefix = _tmux_prefix()
    if not is_destroyable_terminal_session(session_name, prefix):
        error = ErrorResponse(detail=f"Refusing to destroy non-terminal session: {session_name!r}")
        return _json_response(error.model_dump(), status_code=400)
    # ``=`` forces an exact session-name match so tmux's prefix fallback can't
    # target a different session.
    result = run_local_command_modern_version(
        command=["tmux", "kill-session", "-t", f"={session_name}"],
        cwd=None,
        is_checked=False,
        timeout=5.0,
    )
    # tmux returns non-zero both for a genuine failure and for an already-absent
    # session (nothing to kill). Distinguish the two by re-listing: if the
    # session is gone, the destroy succeeded (or was idempotent); if it is still
    # present, the kill really failed and we must surface it rather than telling
    # the UI the terminal is gone when it is still running.
    if result.returncode != 0:
        still_live = any(session.session_name == session_name for session in _list_tmux_sessions())
        if still_live:
            _loguru_logger.warning("Failed to kill terminal session {}: {}", session_name, result.stderr.strip())
            error = ErrorResponse(detail=f"Failed to destroy terminal {session_name!r}: {result.stderr.strip()}")
            return _json_response(error.model_dump(), status_code=500)
    with _terminal_allocate_lock:
        _recently_allocated_terminal_names.discard(session_name)
    return _json_response({"status": "ok"})


def _get_terminal_banner_dismissed() -> Response:
    """Whether the user has permanently dismissed the terminal lifecycle banner."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        return _json_response({"dismissed": False})
    banner_file = layout_dir / _TERMINAL_BANNER_FILENAME
    if not banner_file.exists():
        return _json_response({"dismissed": False})
    try:
        data = json.loads(banner_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _loguru_logger.opt(exception=e).warning(
            "Failed to read terminal banner state at {}; treating as not dismissed", banner_file
        )
        return _json_response({"dismissed": False})
    dismissed = bool(data.get("dismissed", False)) if isinstance(data, dict) else False
    return _json_response({"dismissed": dismissed})


def _set_terminal_banner_dismissed() -> Response:
    """Persist the user's "never show again" choice for the terminal banner."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return _json_response(error.model_dump(), status_code=500)
    try:
        body = json.loads(request.get_data() or b"{}")
    except (json.JSONDecodeError, ValueError) as e:
        _loguru_logger.opt(exception=e).warning("terminal banner-dismissed received invalid JSON body")
        error = ErrorResponse(detail="Invalid JSON in request body")
        return _json_response(error.model_dump(), status_code=400)
    dismissed = bool(body.get("dismissed", False)) if isinstance(body, dict) else False
    layout_dir.mkdir(parents=True, exist_ok=True)
    (layout_dir / _TERMINAL_BANNER_FILENAME).write_text(json.dumps({"dismissed": dismissed}))
    return _json_response({"dismissed": dismissed})


def _resolve_terminal_id_for_tty(client_tty: str) -> str | None:
    """Reverse-look-up the dockview terminal id bound to a tmux client tty.

    ``scripts/run_ttyd.sh`` records ``terminal_id -> $(tty)`` files under
    ``$MNGR_AGENT_STATE_DIR/commands/ttyd/clients/`` when a tab attaches; this
    finds the id whose recorded tty matches ``client_tty``. Returns None when
    the mapping directory or a matching entry is absent.
    """
    if not client_tty:
        return None
    state_dir = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir:
        return None
    clients_dir = Path(state_dir) / "commands" / "ttyd" / "clients"
    if not clients_dir.is_dir():
        return None
    for entry in clients_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            recorded_tty = entry.read_text().strip()
        except OSError:
            continue
        if recorded_tty == client_tty:
            return entry.name
    return None


def _terminal_notify_endpoint() -> Response:
    """Loopback endpoint the tmux hooks call when a terminal's session changes.

    Body: ``{kind, client_tty, session_name, session_id}``. For a session
    switch we resolve the affected dockview tab from ``client_tty``; for a
    rename the frontend matches by ``session_id`` so ``terminal_id`` stays None.
    Either way we re-broadcast as a ``terminal_session`` WS event.
    """
    client_host = request.remote_addr or ""
    if client_host not in _LOOPBACK_CLIENT_HOSTS:
        error = ErrorResponse(detail="terminal notify is only callable from loopback")
        return _json_response(error.model_dump(), status_code=403)
    try:
        body = json.loads(request.get_data())
    except (json.JSONDecodeError, ValueError) as e:
        _loguru_logger.opt(exception=e).warning("terminal notify received invalid JSON body")
        error = ErrorResponse(detail="Invalid JSON in request body")
        return _json_response(error.model_dump(), status_code=400)
    if not isinstance(body, dict):
        error = ErrorResponse(detail="Request body must be a JSON object")
        return _json_response(error.model_dump(), status_code=400)
    kind = body.get("kind")
    session_name = str(body.get("session_name") or "")
    session_id = str(body.get("session_id") or "")
    client_tty = str(body.get("client_tty") or "")
    broadcaster: WebSocketBroadcaster = get_state().broadcaster
    if kind == "session-changed":
        # Resolve which dockview tab this tmux client belongs to. An
        # unresolved tty (e.g. an mngr agent-session client, which never
        # writes the ttyd clients map) means there is no terminal tab to
        # update, so skip the broadcast entirely.
        terminal_id = _resolve_terminal_id_for_tty(client_tty)
        if terminal_id is None:
            return _json_response({"ok": True, "broadcast": False})
        broadcaster.broadcast_terminal_session(terminal_id, session_id, session_name)
        return _json_response({"ok": True, "broadcast": True})
    if kind == "session-renamed":
        # A rename has no client context; the frontend matches the affected
        # tab by ``session_id``.
        broadcaster.broadcast_terminal_session(None, session_id, session_name)
        return _json_response({"ok": True, "broadcast": True})
    error = ErrorResponse(detail=f"Unknown terminal notify kind: {kind!r}")
    return _json_response(error.model_dump(), status_code=400)


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
    # Resolve the primary agent's layout dir once, at connect, and bind it to
    # this connection for the lifetime of the loop. The resolver reads
    # process-global env (MNGR_HOST_DIR / MNGR_AGENT_ID); capturing it here
    # keeps every write this connection makes pointed at *this* server's
    # workspace even if that env is later mutated (which only happens in tests,
    # where several servers share one process -- a stray late write from a
    # lingering connection would otherwise land in another server's log).
    _run_ws_broadcast_loop(
        websocket=websocket,
        agent_manager=state.agent_manager,
        ws_broadcaster=state.broadcaster,
        layout_dir=_primary_agent_layout_dir(),
    )


def _handle_client_state_message(
    raw_message: str,
    client_queue: "queue.Queue[str | None]",
    ws_broadcaster: WebSocketBroadcaster,
    layout_dir: Path | None,
    is_first_report: bool,
) -> bool:
    """Process one incoming WebSocket message; returns True for a ``client_state``.

    ``client_state`` is the only message type clients send: it registers the
    browser's client id, active layout, and device kind (on connect and on
    every layout switch). Registration feeds the broadcaster's client
    registry (used to target layout-mutating ops), the last-active-layout
    record, and the client-activity event log (a ``layout_switch`` event when
    the report names a different previous layout, else a ``client_connected``
    event for the connection's first report).
    """
    try:
        parsed = json.loads(raw_message)
    except json.JSONDecodeError as e:
        _loguru_logger.opt(exception=e).warning("Ignored unparsable WebSocket message from client")
        return False
    if not isinstance(parsed, dict) or parsed.get("type") != "client_state":
        _loguru_logger.warning("Ignored unexpected WebSocket message type from client: {!r}", parsed)
        return False
    client_id = str(parsed.get("client_id") or "")
    active_layout = str(parsed.get("active_layout") or "")
    device_kind = str(parsed.get("device_kind") or "")
    previous_layout = str(parsed.get("previous_layout") or "")
    if not client_id or not active_layout:
        return False
    ws_broadcaster.set_client_info(client_queue, client_id, active_layout, device_kind)
    if layout_dir is not None:
        workspace_layouts.set_last_active_slug(layout_dir, active_layout)
        events_path = client_activity.get_events_path(layout_dir)
        if previous_layout and previous_layout != active_layout:
            client_activity.append_layout_switch_event(
                events_path, client_id, device_kind, previous_layout, active_layout
            )
        elif is_first_report:
            client_activity.append_client_connected_event(events_path, client_id, device_kind, active_layout)
        else:
            # A re-report on an already-registered connection with an
            # unchanged layout; the registry update above is all it needs.
            pass
    return True


def _run_ws_broadcast_loop(
    websocket: Any,
    agent_manager: AgentManager,
    ws_broadcaster: WebSocketBroadcaster,
    layout_dir: Path | None,
) -> None:
    """Stream broadcaster messages to ``websocket`` until the client disconnects.

    Each WebSocket connection owns its own thread (flask-sock + the threaded
    WSGI server), so this loop simply blocks on the per-client queue and
    forwards messages. flask-sock's ``ping_interval`` keepalive closes a
    half-dead peer, surfacing as ``ConnectionClosed`` from ``send``; the
    broadcaster can also evict a hopelessly-behind client by pushing the
    shutdown sentinel (``None``) into the queue.

    Incoming ``client_state`` registrations are drained non-blockingly on
    each loop iteration (simple_websocket buffers frames on its own reader
    thread, so ``receive(timeout=0)`` never blocks); worst-case processing
    latency is one queue-poll interval (~1 s), well under any agent-driven
    op that depends on the registration.
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

        is_client_registered = False
        shutdown = False
        while not shutdown:
            incoming = websocket.receive(timeout=0)
            while incoming is not None:
                if _handle_client_state_message(
                    str(incoming),
                    client_queue,
                    ws_broadcaster,
                    layout_dir=layout_dir,
                    is_first_report=not is_client_registered,
                ):
                    is_client_registered = True
                incoming = websocket.receive(timeout=0)
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
    the bootstrap, web, cloudflared, and other supervised services
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


def _resolve_requested_layout_slug(
    args_raw: dict[str, Any],
    layout_dir: Path | None,
) -> tuple[str | None, Response | None]:
    """Resolve a layout op's ``args.layout`` (or the last-active default) to a slug.

    Returns ``(slug, None)`` on success and ``(None, error_response)`` when an
    explicitly-named layout is unusable or unknown. With no layout dir
    configured (dev/test), an explicit name is slugified without registry
    validation and the default is None.
    """
    requested = args_raw.get("layout")
    if isinstance(requested, str) and requested:
        if layout_dir is None:
            try:
                return workspace_layouts.slugify_layout_name(requested), None
            except workspace_layouts.LayoutNameError as e:
                return None, _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=400)
        try:
            return workspace_layouts.resolve_layout_slug(layout_dir, requested), None
        except workspace_layouts.LayoutNameError as e:
            return None, _json_response(ErrorResponse(detail=str(e)).model_dump(), status_code=400)
        except workspace_layouts.LayoutNotFoundError:
            known = ", ".join(info.display_name for info in workspace_layouts.list_layouts(layout_dir))
            error = ErrorResponse(detail=f"Layout {requested!r} not found (known layouts: {known})")
            return None, _json_response(error.model_dump(), status_code=404)
    if layout_dir is None:
        return None, None
    return workspace_layouts.get_last_active_slug(layout_dir), None


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
    layout_dir = _primary_agent_layout_dir()

    if op in {"list", "inspect"}:
        slug, error_response = _resolve_requested_layout_slug(args_raw, layout_dir)
        if error_response is not None:
            return error_response
        layout_path = (
            workspace_layouts.layout_content_path(layout_dir, slug)
            if layout_dir is not None and slug is not None
            else None
        )
        if op == "list":
            entries = layout_list(
                agent_manager.list_service_names(),
                agent_manager.get_agents_serialized(),
                layout_path,
                agent_name_by_id,
            )
            # Log the caller for telemetry; v1 has no enforcement.
            logger.info("layout op={} agent_id={} layout={} entries={}", op, agent_id, slug, len(entries))
            return _json_response({"ok": True, "layout_slug": slug, "entries": entries})
        summary = layout_inspect(layout_path, agent_name_by_id)
        logger.info("layout op={} agent_id={} layout={} panels={}", op, agent_id, slug, len(summary.get("panels", [])))
        return _json_response({"ok": True, "layout_slug": slug, "layout": summary})

    if op == "context":
        # Per-client activity summary: who is connected, on which layout,
        # and what they recently asked for. The live registry overrides the
        # event-log-derived current layout for connected clients (fresher,
        # and correct even if an event write was skipped).
        events_path = _client_activity_events_path()
        events = client_activity.read_client_activity_events(events_path) if events_path is not None else []
        connected_infos = get_state().broadcaster.get_connected_client_infos()
        live_layout_by_client_id = {info["client_id"]: info["active_layout_slug"] for info in connected_infos}
        clients = client_activity.summarize_client_activity(events, set(live_layout_by_client_id))
        for client_summary in clients:
            live_layout = live_layout_by_client_id.get(client_summary["client_id"])
            if live_layout:
                client_summary["current_layout"] = live_layout
        logger.info("layout op={} agent_id={} clients={}", op, agent_id, len(clients))
        return _json_response({"ok": True, "clients": clients})

    if op == "load":
        requested = args_raw.get("layout")
        if not isinstance(requested, str) or not requested:
            error = ErrorResponse(detail="'load' requires a layout name in args.layout")
            return _json_response(error.model_dump(), status_code=400)
        if layout_dir is None:
            error = ErrorResponse(detail="No primary agent configured for this workspace")
            return _json_response(error.model_dump(), status_code=500)
        slug, error_response = _resolve_requested_layout_slug(args_raw, layout_dir)
        if error_response is not None:
            return error_response
        if slug is None:
            # Unreachable: an explicit layout name (validated above) always
            # resolves to a slug or an error response.
            error = ErrorResponse(detail="Failed to resolve the requested layout")
            return _json_response(error.model_dump(), status_code=500)
        display_name = workspace_layouts.get_layout_display_name(layout_dir, slug)
        # Target the explicitly-named client, else the client that most
        # recently messaged the requesting agent, else every client.
        explicit_client = args_raw.get("client")
        if isinstance(explicit_client, str) and explicit_client:
            target_client_id: str | None = explicit_client
        else:
            events_path = _client_activity_events_path()
            events = client_activity.read_client_activity_events(events_path) if events_path is not None else []
            target_client_id = client_activity.find_client_id_for_agent(events, agent_id)
        get_state().broadcaster.broadcast_load_layout(slug, display_name, target_client_id)
        logger.info("layout op={} agent_id={} layout={} target_client={}", op, agent_id, slug, target_client_id)
        return _json_response({"ok": True, "layout": slug, "target_client_id": target_client_id})

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
        # Mutating ops are layout-targeted: they require an explicit target
        # layout and are delivered only to connected clients that have it
        # active (those clients apply the mutation and autosave it into the
        # named layout's file). With no such client the op cannot take
        # effect anywhere, so fail loudly rather than broadcasting into the
        # void.
        requested_layout = args_raw.get("layout")
        if not isinstance(requested_layout, str) or not requested_layout:
            error = ErrorResponse(detail=f"Layout op {op!r} requires a target layout (pass --layout)")
            return _json_response(error.model_dump(), status_code=400)
        target_layout_slug, layout_error_response = _resolve_requested_layout_slug(args_raw, layout_dir)
        if layout_error_response is not None:
            return layout_error_response
        if target_layout_slug is None or not broadcaster.has_client_on_layout(target_layout_slug):
            error = ErrorResponse(
                detail=(
                    f"No connected client has layout '{requested_layout}' active. Ask the user to switch "
                    f"to it, or run `layout.py load {requested_layout!r}` first."
                )
            )
            return _json_response(error.model_dump(), status_code=412)
        broadcast_args = {key: value for key, value in args_raw.items() if key != "layout"}
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
            broadcaster.broadcast_layout_op(
                op, broadcast_args, requester_agent_id=agent_id, target_layout_slug=target_layout_slug
            )
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


def create_application(state: SystemInterfaceState) -> Flask:
    """Assemble the Flask app around an already-built ``SystemInterfaceState``.

    Pure assembler: it wires routes, plugins, and error handling onto the app
    and attaches the injected ``state``. It constructs no collaborators and
    starts nothing. The composition root (``main.build_production_state`` plus
    ``main.main``) builds the real object graph and starts the agent manager;
    tests build a ``SystemInterfaceState`` with fakes via
    ``testing.build_test_state`` and pass it here.
    """
    # static_folder=None disables Flask's default /static route; the system
    # interface serves its own static assets explicitly below.
    application = Flask(__name__, static_folder=None)
    application.config["SOCK_SERVER_OPTIONS"] = {
        "ping_interval": _WS_PING_INTERVAL_SECONDS,
        # Echo back whatever subprotocol the client offered so the WS proxy is
        # transparent (e.g. ttyd's ``tty``); see ``_ReflectClientSubprotocols``.
        "subprotocols": _ReflectClientSubprotocols(),
    }
    attach_state(application, state)

    plugin_manager = get_plugin_manager()
    plugin_manager.hook.register_event_broadcaster(broadcaster=state.event_queues.broadcast)

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
    application.add_url_rule(
        "/api/agents/<agent_id>/model-settings", view_func=_get_model_settings_endpoint, methods=["GET"]
    )
    application.add_url_rule("/api/agents/<agent_id>/model", view_func=_set_model_endpoint, methods=["POST"])
    application.add_url_rule("/api/agents/<agent_id>/fast", view_func=_set_fast_mode_endpoint, methods=["POST"])
    application.add_url_rule("/api/activity", view_func=_activity_endpoint, methods=["POST"])
    application.add_url_rule("/api/uploads", view_func=_upload_attachment, methods=["POST"])
    application.add_url_rule("/api/uploads/<path:relative_path>", view_func=_serve_attachment, methods=["GET"])
    application.add_url_rule(
        "/api/uploads/<path:relative_path>",
        view_func=_delete_attachment,
        methods=["DELETE"],
        endpoint="_delete_attachment",
    )
    application.add_url_rule("/api/agents/<agent_id>/interrupt", view_func=_interrupt_agent_endpoint, methods=["POST"])
    application.add_url_rule("/api/layouts", view_func=_list_layouts_endpoint, methods=["GET"])
    application.add_url_rule(
        "/api/layouts", view_func=_save_layout_as_endpoint, methods=["POST"], endpoint="_save_layout_as"
    )
    application.add_url_rule("/api/layouts/<slug>", view_func=_get_named_layout_endpoint, methods=["GET"])
    application.add_url_rule(
        "/api/layouts/<slug>",
        view_func=_autosave_named_layout_endpoint,
        methods=["POST"],
        endpoint="_autosave_named_layout",
    )
    application.add_url_rule("/api/layouts/<slug>/delete", view_func=_delete_named_layout_endpoint, methods=["POST"])
    application.add_url_rule("/api/agents/<agent_id>/screen", view_func=_get_screen_capture, methods=["GET"])
    application.add_url_rule("/api/agents/<agent_id>/destroy", view_func=_destroy_agent, methods=["POST"])
    application.add_url_rule("/api/agents/<agent_id>/start", view_func=_start_agent, methods=["POST"])
    application.add_url_rule("/api/terminals", view_func=_list_terminals, methods=["GET"])
    application.add_url_rule("/api/terminals/allocate", view_func=_allocate_terminal, methods=["POST"])
    application.add_url_rule(
        "/api/terminals/banner-dismissed",
        view_func=_get_terminal_banner_dismissed,
        methods=["GET"],
    )
    application.add_url_rule(
        "/api/terminals/banner-dismissed",
        view_func=_set_terminal_banner_dismissed,
        methods=["POST"],
        endpoint="_set_terminal_banner_dismissed",
    )
    application.add_url_rule(
        "/api/terminals/<session_name>/destroy",
        view_func=_destroy_terminal,
        methods=["POST"],
    )
    application.add_url_rule("/api/terminals/notify", view_func=_terminal_notify_endpoint, methods=["POST"])
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
