import json
import os
import queue
import signal
import socket
import threading
import traceback
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger as _loguru_logger
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket
from starlette.websockets import WebSocketDisconnect

from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.mngr.errors import MngrError
from imbue.system_interface import claude_auth_endpoints
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import discover_agents
from imbue.system_interface.agent_discovery import get_host_dir
from imbue.system_interface.agent_discovery import read_claude_config_dir_from_env_file
from imbue.system_interface.agent_discovery import read_tickets_dir_from_env_file
from imbue.system_interface.agent_discovery import send_message
from imbue.system_interface.agent_discovery import start_agent
from imbue.system_interface.agent_manager import AgentManager
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
from imbue.system_interface.session_watcher import AgentSessionWatcher
from imbue.system_interface.tickets_watcher import AgentTicketsWatcher
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


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan.

    Reads ``application.state.preconfigured_agent_manager`` (set up by
    ``create_application``). When present, the lifespan reuses that
    manager and does not call ``start()`` / ``stop()`` -- this is the
    hook tests use to seed the service registry without spawning the
    real ``mngr observe`` pipeline. When absent, the lifespan builds a
    fresh manager and owns its lifecycle.
    """
    event_queues = AgentEventQueues()
    application.state.event_queues = event_queues

    preconfigured_agent_manager: AgentManager | None = application.state.preconfigured_agent_manager
    if preconfigured_agent_manager is None:
        broadcaster = WebSocketBroadcaster()
        agent_manager = AgentManager.build(broadcaster)
        agent_manager.start()
    else:
        agent_manager = preconfigured_agent_manager
        broadcaster = agent_manager.broadcaster

    application.state.broadcaster = broadcaster
    application.state.agent_manager = agent_manager
    # Advisory in-process mutex serializing layout-mutating ops. The agent
    # script never auto-retries on contention -- it surfaces the 409 to the
    # agent along with the in-flight holder's metadata.
    application.state.layout_mutex = LayoutMutex()

    # Single shared httpx client for the /service/<name>/ forwarding layer.
    application.state.http_client = httpx.AsyncClient(
        follow_redirects=False,
        timeout=30.0,
    )

    plugin_manager = get_plugin_manager()
    plugin_manager.hook.register_event_broadcaster(broadcaster=event_queues.broadcast)

    is_main_thread = threading.current_thread() is threading.main_thread()
    original_sigint_handler = None

    if is_main_thread:
        original_sigint_handler = signal.getsignal(signal.SIGINT)

        def _graceful_shutdown_handler(signum: int, frame: object) -> None:
            event_queues.shutdown()
            broadcaster.shutdown()
            if preconfigured_agent_manager is None:
                agent_manager.stop()
            _stop_all_watchers(application)
            handler = original_sigint_handler
            if callable(handler):
                handler(signum, frame)  # type: ignore[arg-type]

        signal.signal(signal.SIGINT, _graceful_shutdown_handler)

    yield

    event_queues.shutdown()
    broadcaster.shutdown()
    if preconfigured_agent_manager is None:
        agent_manager.stop()
    _stop_all_watchers(application)
    await application.state.http_client.aclose()
    if is_main_thread and original_sigint_handler is not None:
        signal.signal(signal.SIGINT, original_sigint_handler)


def _stop_all_watchers(application: FastAPI) -> None:
    watchers: dict[str, AgentSessionWatcher] = application.state.watchers
    for watcher in watchers.values():
        watcher.stop()
    watchers.clear()
    tickets_watchers: dict[str, AgentTicketsWatcher] = application.state.tickets_watchers
    for tickets_watcher in tickets_watchers.values():
        tickets_watcher.stop()
    tickets_watchers.clear()


def _get_or_create_watcher(request: Request, agent_info: AgentInfo) -> AgentSessionWatcher:
    """Get an existing watcher for an agent, or create one."""
    watchers: dict[str, AgentSessionWatcher] = request.app.state.watchers
    event_queues: AgentEventQueues = request.app.state.event_queues
    agent_manager: AgentManager = request.app.state.agent_manager

    if agent_info.id in watchers:
        return watchers[agent_info.id]

    # Single-element holder so the ``on_events`` closure can reach the watcher
    # we are about to construct. Capturing the watcher directly (rather than
    # looking it up in ``watchers`` by id on every event) keeps the callback
    # self-contained: it cannot KeyError if the dict entry has since been
    # removed, and it does not depend on the implicit invariant that the
    # entry was already inserted before the first event fires.
    watcher_holder: list[AgentSessionWatcher] = []

    def on_events(agent_id: str, events: list[dict[str, Any]]) -> None:
        # IGNORE: session events are persisted in JSONL and recoverable via
        # the REST /events endpoint; storing them in the in-memory replay
        # buffer would grow unboundedly for the agent's lifetime.
        event_queues.broadcast_all_ignored(agent_id, events)
        # Recompute the per-agent activity state from the full transcript.
        # The session watcher's incremental ``events`` argument only contains
        # the newest lines, but the activity tracker needs the full transcript
        # to detect unmatched tool_uses across turns and to read the last
        # event's type.
        agent_manager.update_session_events(agent_id, watcher_holder[0].get_all_events())

    watcher = AgentSessionWatcher(
        agent_id=agent_info.id,
        agent_state_dir=agent_info.agent_state_dir,
        claude_config_dir=agent_info.claude_config_dir,
        on_events=on_events,
    )
    watcher_holder.append(watcher)
    watchers[agent_info.id] = watcher
    watcher.start()
    # Seed transcript-derived activity signals once at watcher creation so the
    # indicator does not lag a turn behind on first connect.
    agent_manager.update_session_events(agent_info.id, watcher.get_all_events())
    return watcher


def _get_or_create_tickets_watcher(request: Request, agent_info: AgentInfo) -> AgentTicketsWatcher | None:
    """Get an existing tickets watcher for an agent, or create one. Returns
    None if the agent has no resolvable working directory (in which case
    there's no .tickets/ to watch)."""
    if agent_info.work_dir is None:
        return None

    tickets_watchers: dict[str, AgentTicketsWatcher] = request.app.state.tickets_watchers
    event_queues: AgentEventQueues = request.app.state.event_queues

    if agent_info.id in tickets_watchers:
        return tickets_watchers[agent_info.id]

    # The tickets watcher emits a single `step_enrichment` snapshot message whenever
    # ticket state changes. ``broadcast_all_ignored`` delivers it live without buffering:
    # it is a full snapshot recomputed on every GET /events (via get_enrichment()), so
    # buffering successive snapshots would grow unboundedly for no benefit. The frontend
    # replaces its enrichment table each time one arrives.
    watcher = AgentTicketsWatcher(
        agent_id=agent_info.id,
        agent_name=agent_info.name,
        tickets_dir=read_tickets_dir_from_env_file(agent_info.agent_state_dir, Path(agent_info.work_dir)),
        on_events=event_queues.broadcast_all_ignored,
    )
    tickets_watchers[agent_info.id] = watcher
    watcher.start()
    return watcher


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


def _index(request: Request) -> Response:
    index_path = STATIC_DIRECTORY / "index.html"
    if index_path.exists():
        config: Config = request.app.state.config
        root_path = request.scope.get("root_path", "").rstrip("/")
        html_content = index_path.read_text()
        html_content = _inject_base_path_meta_tag(html_content, root_path)
        html_content = _inject_hostname_meta_tag(html_content)
        html_content = _inject_agent_id_meta_tag(html_content)
        if config.javascript_plugin_basenames:
            html_content = _inject_plugin_script_tags(html_content, config.javascript_plugin_basenames, root_path)
        return HTMLResponse(html_content)
    return HTMLResponse(_FRONTEND_NOT_BUILT_HTML)


def _favicon() -> Response:
    favicon_path = STATIC_DIRECTORY / "favicon.ico"
    if favicon_path.exists():
        return FileResponse(favicon_path, media_type="image/x-icon")
    return Response(status_code=404)


def _discover_with_filters(request: Request) -> list[AgentInfo]:
    """Discover agents using the app-level filter configuration."""
    return discover_agents(
        provider_names=request.app.state.provider_names,
        include_filters=request.app.state.include_filters,
        exclude_filters=request.app.state.exclude_filters,
    )


def _list_agents_endpoint(request: Request) -> JSONResponse:
    """List all mngr-managed agents."""
    agents = _discover_with_filters(request)
    items = [AgentListItem(id=agent.id, name=agent.name, state=agent.state) for agent in agents]
    return JSONResponse(content=AgentListResponse(agents=items).model_dump())


def _find_agent(agent_id: str, request: Request) -> AgentInfo | None:
    """Find a specific agent by ID.

    Uses the AgentManager's already-loaded state instead of running a full
    mngr discovery on every request.  Falls back to the agent state directory
    for claude_config_dir resolution.
    """
    agent_manager: AgentManager = request.app.state.agent_manager
    agent_state = agent_manager.get_agent_by_id(agent_id)
    if agent_state is None:
        return None

    host_dir = get_host_dir()
    agent_state_dir = host_dir / "agents" / agent_id
    claude_config_dir = read_claude_config_dir_from_env_file(agent_state_dir)

    return AgentInfo(
        id=agent_state.id,
        name=agent_state.name,
        state=agent_state.state,
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        labels=agent_state.labels,
        work_dir=agent_state.work_dir,
    )


def _agent_not_found_response(agent_id: str) -> JSONResponse:
    error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
    return JSONResponse(content=error.model_dump(), status_code=404)


def _get_events(agent_id: str, request: Request) -> Response:
    """Get events for an agent. Supports tail-first loading and backfill."""
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    before_event_id = request.query_params.get("before")
    after_event_id = request.query_params.get("after")
    offset_str = request.query_params.get("offset")
    limit_str = request.query_params.get("limit", str(_DEFAULT_TAIL_COUNT))
    try:
        limit = int(limit_str)
    except ValueError:
        limit = _DEFAULT_TAIL_COUNT
    # A non-positive limit would defeat the window cap and break slicing, so fall
    # back to the default.
    if limit <= 0:
        limit = _DEFAULT_TAIL_COUNT

    watcher = _get_or_create_watcher(request, agent_info)
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
    # tk step enrichment ships as a separate, unpaginated snapshot (always
    # complete regardless of where the transcript window is), joined to the
    # transcript-derived steps by id on the frontend.
    tickets_watcher = _get_or_create_tickets_watcher(request, agent_info)
    step_enrichment = tickets_watcher.get_enrichment() if tickets_watcher is not None else {}
    return JSONResponse(
        content={"events": events, "offset": offset, "total": total, "step_enrichment": step_enrichment}
    )


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


def _stream_events(agent_id: str, request: Request) -> Response:
    """SSE stream for an agent's new events."""
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    watcher = _get_or_create_watcher(request, agent_info)
    _get_or_create_tickets_watcher(request, agent_info)

    event_queues: AgentEventQueues = request.app.state.event_queues
    event_queue = event_queues.register(agent_id)

    return StreamingResponse(
        _stream_filtered_events(agent_id, event_queues, event_queue, watcher.is_main_session_event),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


def _send_message_endpoint(agent_id: str, send_message_request: SendMessageRequest, request: Request) -> JSONResponse:
    """Send a message to an agent."""
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    success = send_message(agent_info.name, send_message_request.message)
    if not success:
        error = ErrorResponse(detail=f"Failed to send message to agent '{agent_info.name}' (0 successful agents)")
        return JSONResponse(content=error.model_dump(), status_code=500)

    return JSONResponse(content=SendMessageResponse(status="ok").model_dump())


async def _interrupt_agent_endpoint(agent_id: str, request: Request) -> JSONResponse:
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
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    if agent_info.labels.get("is_primary") == "true":
        error = ErrorResponse(
            detail=(
                f"Refusing to interrupt agent '{agent_info.name}': it carries "
                "the is_primary=true label (services agent for this workspace)"
            )
        )
        return JSONResponse(content=error.model_dump(), status_code=400)

    agent_name = agent_info.name

    def _run_restart() -> tuple[bool, str]:
        result = run_local_command_modern_version(
            command=["mngr", "start", agent_name, "--restart", "--no-resume"],
            cwd=None,
            is_checked=False,
            timeout=60.0,
        )
        succeeded = result.returncode == 0
        output = result.stdout.strip() if succeeded else result.stderr.strip()
        return succeeded, output

    success, output = await run_in_threadpool(_run_restart)
    if not success:
        error = ErrorResponse(detail=f"Failed to interrupt agent '{agent_name}': {output}")
        return JSONResponse(content=error.model_dump(), status_code=500)

    # The restart abandons the session transcript mid-turn, so the
    # transcript-derived activity state would stay pinned at THINKING /
    # TOOL_RUNNING until the user sends another message. Reset it to IDLE
    # now so the activity indicator clears immediately after the stop.
    agent_manager: AgentManager = request.app.state.agent_manager
    agent_manager.reset_activity_state(agent_id)

    return JSONResponse(content=InterruptAgentResponse(status="ok").model_dump())


def _get_subagent_events(agent_id: str, subagent_session_id: str, request: Request) -> Response:
    """Get events for a specific subagent session."""
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    watcher = _get_or_create_watcher(request, agent_info)
    events = watcher.get_all_events(session_id=subagent_session_id)

    # Include metadata in the response
    metadata = watcher.get_subagent_metadata(subagent_session_id)

    return JSONResponse(content={"events": events, "metadata": metadata})


def _stream_subagent_events(agent_id: str, subagent_session_id: str, request: Request) -> Response:
    """SSE stream for a subagent's new events, filtered by session_id."""
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    _get_or_create_watcher(request, agent_info)

    event_queues: AgentEventQueues = request.app.state.event_queues
    event_queue = event_queues.register(agent_id)

    return StreamingResponse(
        _stream_filtered_events(
            agent_id,
            event_queues,
            event_queue,
            lambda event: event.get("session_id") == subagent_session_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
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
        return JSONResponse(content=None, status_code=404)

    layout_file = layout_dir / _LAYOUT_FILENAME
    if not layout_file.exists():
        return JSONResponse(content=None, status_code=404)

    try:
        layout_data = json.loads(layout_file.read_text())
        return JSONResponse(content=layout_data)
    except (json.JSONDecodeError, OSError):
        return JSONResponse(content=None, status_code=404)


async def _save_layout(request: Request) -> Response:
    """Save the workspace layout for this workspace's primary agent."""
    layout_dir = _primary_agent_layout_dir()
    if layout_dir is None:
        error = ErrorResponse(detail="No primary agent configured for this workspace")
        return JSONResponse(content=error.model_dump(), status_code=500)

    try:
        body = await request.body()
        # Validate it's valid JSON
        json.loads(body)
    except (json.JSONDecodeError, ValueError):
        error = ErrorResponse(detail="Invalid JSON in request body")
        return JSONResponse(content=error.model_dump(), status_code=400)

    layout_dir.mkdir(parents=True, exist_ok=True)
    layout_file = layout_dir / _LAYOUT_FILENAME
    layout_file.write_bytes(body)

    return JSONResponse(content={"status": "ok"})


async def _get_screen_capture(agent_id: str, request: Request) -> Response:
    """Capture the tmux pane content for an agent.

    Returns the visible screen content (and optionally scrollback) as plain
    text. Useful for seeing what's on an agent's terminal when it has no
    Claude session data (e.g., the agent crashed on startup).
    """
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    prefix = os.environ.get("MNGR_PREFIX", "mngr-")
    session_name = f"{prefix}{agent_info.name}"
    include_scrollback = request.query_params.get("scrollback", "false").lower() == "true"
    scrollback_flag = ["-S", "-"] if include_scrollback else []
    command = ["tmux", "capture-pane", "-t", session_name, *scrollback_flag, "-p"]

    def _run_capture() -> tuple[bool, str]:
        result = run_local_command_modern_version(
            command=command,
            cwd=None,
            is_checked=False,
            timeout=5.0,
        )
        succeeded = result.returncode == 0
        return succeeded, result.stdout if succeeded else result.stderr

    success, output = await run_in_threadpool(_run_capture)
    if not success:
        return JSONResponse(
            content={"screen": None, "error": f"tmux session not found: {session_name}"},
            status_code=200,
        )
    return JSONResponse(content={"screen": output})


def _serve_static_file(basename: str, request: Request) -> Response:
    config: Config = request.app.state.config
    file_path_string = config.static_file_basename_to_path.get(basename)
    if file_path_string is None:
        error = ErrorResponse(detail=f"Static file '{basename}' not found")
        return JSONResponse(content=error.model_dump(), status_code=404)
    file_path = Path(file_path_string)
    if not file_path.is_file():
        error = ErrorResponse(detail=f"Static file not found on disk: {file_path}")
        return JSONResponse(content=error.model_dump(), status_code=404)
    return FileResponse(file_path)


def _random_name_endpoint(request: Request) -> JSONResponse:
    """Generate a random agent name."""
    agent_manager: AgentManager = request.app.state.agent_manager
    name = agent_manager.generate_random_name()
    return JSONResponse(content=RandomNameResponse(name=name).model_dump())


async def _create_worktree_agent(request: Request) -> JSONResponse:
    """Create a new worktree agent."""
    agent_manager: AgentManager = request.app.state.agent_manager
    body = await request.json()

    try:
        create_request = CreateWorktreeRequest(**body)
        agent_name = create_request.name
        selected_agent_id = create_request.selected_agent_id or agent_manager.get_own_agent_id()
        agent_id = agent_manager.create_worktree_agent(agent_name, selected_agent_id)
        return JSONResponse(
            content=CreateAgentResponse(agent_id=agent_id).model_dump(),
            status_code=201,
        )
    except (AgentCreationError, OSError, ValueError) as e:
        error = ErrorResponse(detail=str(e))
        return JSONResponse(content=error.model_dump(), status_code=400)


async def _create_chat_agent(request: Request) -> JSONResponse:
    """Create a new chat agent in the primary agent's work directory."""
    agent_manager: AgentManager = request.app.state.agent_manager
    body = await request.json()

    try:
        create_request = CreateChatRequest(**body)
        agent_id = agent_manager.create_chat_agent(create_request.name)
        return JSONResponse(
            content=CreateAgentResponse(agent_id=agent_id).model_dump(),
            status_code=201,
        )
    except (AgentCreationError, OSError, ValueError) as e:
        error = ErrorResponse(detail=str(e))
        return JSONResponse(content=error.model_dump(), status_code=400)


async def _ws_endpoint(websocket: WebSocket) -> None:
    """Unified WebSocket for agent state and application updates."""
    await websocket.accept()
    agent_manager: AgentManager = websocket.app.state.agent_manager
    ws_broadcaster: WebSocketBroadcaster = websocket.app.state.broadcaster
    await _run_ws_broadcast_loop(
        websocket=websocket,
        agent_manager=agent_manager,
        ws_broadcaster=ws_broadcaster,
    )


async def _run_ws_broadcast_loop(
    websocket: WebSocket,
    agent_manager: AgentManager,
    ws_broadcaster: WebSocketBroadcaster,
) -> None:
    """Stream broadcaster messages to ``websocket`` until the client disconnects.

    A wedged ``websocket.send_text`` (eg. a half-dead TCP connection) is freed
    by the broadcaster: ``register`` captures the current asyncio Task and
    loop, and when this client's queue racks up enough consecutive overflow
    broadcasts the broadcaster cancels the task via
    ``loop.call_soon_threadsafe``. ``CancelledError`` propagates into the
    blocked send and unwinds through the ``finally`` below, which unregisters
    the queue. There is no per-send wall-clock timeout.
    """
    client_queue = ws_broadcaster.register()
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "agents_updated",
                    "agents": agent_manager.get_agents_serialized(),
                }
            )
        )
        await websocket.send_text(
            json.dumps(
                {
                    "type": "applications_updated",
                    "applications": agent_manager.get_applications_serialized(),
                }
            )
        )

        for proto in agent_manager.get_proto_agents():
            await websocket.send_text(json.dumps({"type": "proto_agent_created", **proto}))

        shutdown = False
        while not shutdown:
            try:
                message = await run_in_threadpool(client_queue.get, timeout=1.0)
            except queue.Empty:
                continue
            if message is None:
                shutdown = True
            else:
                await websocket.send_text(message)
    except WebSocketDisconnect:
        pass
    finally:
        ws_broadcaster.unregister(client_queue)


async def _proto_agent_logs_endpoint(websocket: WebSocket) -> None:
    """WebSocket for streaming proto-agent creation logs."""
    await websocket.accept()
    agent_manager: AgentManager = websocket.app.state.agent_manager
    agent_id = websocket.path_params.get("agent_id", "")
    log_queue = agent_manager.get_log_queue(agent_id)
    await _run_proto_agent_logs_loop(
        websocket=websocket,
        log_queue=log_queue,
    )


async def _run_proto_agent_logs_loop(
    websocket: WebSocket,
    log_queue: queue.Queue[str | None] | None,
) -> None:
    """Stream ``log_queue`` messages to ``websocket`` until the proto-agent finishes.

    If ``log_queue`` is ``None`` the proto-agent does not exist; send a
    structured not-found error and close the socket. Unlike ``_ws_endpoint``
    this path has no broadcaster behind it, so a half-dead TCP connection can
    keep ``send_text`` parked forever -- accepted as a much narrower failure
    surface than the original broadcaster flood (one stuck task per stuck
    creation, capped by the bounded log queue).
    """
    if log_queue is None:
        await websocket.send_text(json.dumps({"done": True, "success": False, "error": "Proto-agent not found"}))
        await websocket.close()
        return

    try:
        finished = False
        while not finished:
            try:
                message = await run_in_threadpool(log_queue.get, timeout=1.0)
            except queue.Empty:
                continue
            if message is None:
                finished = True
            else:
                await websocket.send_text(message)
    except WebSocketDisconnect:
        pass


def _build_destroy_command(agent_name: str) -> list[str]:
    """Build the ``mngr destroy --force`` argv for one agent.

    Pure: argv assembly only, so the repo<->mngr CLI contract is testable
    against the live CLI without a subprocess (see ``server_test.py``).
    """
    return ["mngr", "destroy", agent_name, "--force"]


async def _destroy_agent(agent_id: str, request: Request) -> JSONResponse:
    """Destroy an agent by running mngr destroy --force.

    Refuses to destroy agents carrying the ``is_primary=true`` label: that's
    the services agent for the workspace, and destroying it would tear down
    the bootstrap, telegram, web, cloudflared, and runtime-backup services
    along with it. The frontend already hides ``is_primary=true`` agents
    from the visible agent list; this is defense-in-depth for callers that
    hit the endpoint directly (curl, scripted use, etc.).
    """
    agent_manager: AgentManager = request.app.state.agent_manager
    agent_state = agent_manager.get_agent_by_id(agent_id)
    if agent_state is None:
        error = ErrorResponse(detail=f"Agent '{agent_id}' not found")
        return JSONResponse(content=error.model_dump(), status_code=404)

    if agent_state.labels.get("is_primary") == "true":
        error = ErrorResponse(
            detail=(
                f"Refusing to destroy agent '{agent_state.name}': it carries "
                "the is_primary=true label (services agent for this workspace)"
            )
        )
        return JSONResponse(content=error.model_dump(), status_code=400)

    agent_name = agent_state.name

    def _run_destroy() -> tuple[bool, str]:
        result = run_local_command_modern_version(
            command=_build_destroy_command(agent_name),
            cwd=None,
            is_checked=False,
            timeout=30.0,
        )
        succeeded = result.returncode == 0
        output = result.stdout.strip() if succeeded else result.stderr.strip()
        return succeeded, output

    success, output = await run_in_threadpool(_run_destroy)
    if not success:
        error = ErrorResponse(detail=f"Failed to destroy agent '{agent_name}': {output}")
        return JSONResponse(content=error.model_dump(), status_code=500)

    # Remove the agent from the system_interface's tracked state immediately
    # so the frontend reflects the destruction without waiting for mngr observe.
    agent_manager.remove_agent(agent_id)

    return JSONResponse(content=DestroyAgentResponse(status="ok").model_dump())


async def _start_agent(agent_id: str, request: Request) -> JSONResponse:
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
    agent_info = _find_agent(agent_id, request)
    if agent_info is None:
        return _agent_not_found_response(agent_id)

    def _run_start() -> str | None:
        try:
            start_agent(agent_info.name)
            return None
        except MngrError as e:
            return str(e)

    error_message = await run_in_threadpool(_run_start)
    if error_message is not None:
        error = ErrorResponse(detail=f"Failed to start agent '{agent_info.name}': {error_message}")
        return JSONResponse(content=error.model_dump(), status_code=500)

    return JSONResponse(content=StartAgentResponse(status="ok").model_dump())


async def _layout_broadcast_endpoint(request: Request) -> JSONResponse:
    """Unified loopback endpoint for the agent-facing ``scripts/layout.py`` helper.

    Body: ``{op, args, agent_id}``.

    Dispatch:

    - ``list`` / ``inspect``: pure server-side queries that read the
      ``agent_manager``'s in-memory service/agent registry plus the
      persisted ``layout.json`` (for ``is_open`` flags / tree layout)
      and return a structured payload. Bypass the mutex.
    - ``refresh``: a state-preserving broadcast that doesn't mutate
      serialized layout. Bypass the mutex.
    - All other ops (``open``, ``focus``, ``split``, ``close``, ``move``,
      ``rename``, ``maximize``, ``restore``, ``replace-url``): acquire
      the advisory mutex first; on contention return HTTP 409 with the
      holder's metadata so the caller can decide whether to retry. On
      success, broadcast the ``layout_op`` WS message and return.

    The endpoint is locked to loopback clients (no authentication exists
    between callers and the system interface inside the container).
    """
    client_host = request.client.host if request.client is not None else ""
    if client_host not in _LOOPBACK_CLIENT_HOSTS:
        error = ErrorResponse(detail="layout broadcast is only callable from loopback")
        return JSONResponse(content=error.model_dump(), status_code=403)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as e:
        _loguru_logger.opt(exception=e).warning("layout broadcast received invalid JSON body")
        error = ErrorResponse(detail="Invalid JSON in request body")
        return JSONResponse(content=error.model_dump(), status_code=400)
    if not isinstance(body, dict):
        error = ErrorResponse(detail="Request body must be a JSON object")
        return JSONResponse(content=error.model_dump(), status_code=400)

    op = body.get("op")
    args_raw = body.get("args", {})
    agent_id = body.get("agent_id") or request.headers.get("X-Mngr-Agent-Id") or ""
    if not isinstance(op, str) or not is_known_op(op):
        error = ErrorResponse(detail=f"Unknown layout op: {op!r}")
        return JSONResponse(content=error.model_dump(), status_code=400)
    if not isinstance(args_raw, dict):
        error = ErrorResponse(detail="``args`` must be a JSON object")
        return JSONResponse(content=error.model_dump(), status_code=400)

    agent_manager: AgentManager = request.app.state.agent_manager
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
        return JSONResponse(content={"ok": True, "entries": entries})

    if op == "inspect":
        layout_dir = _primary_agent_layout_dir()
        layout_path = (layout_dir / _LAYOUT_FILENAME) if layout_dir is not None else None
        summary = layout_inspect(layout_path, agent_name_by_id)
        logger.info("layout op={} agent_id={} panels={}", op, agent_id, len(summary.get("panels", [])))
        return JSONResponse(content={"ok": True, "layout": summary})

    if not is_broadcasting_op(op):
        # Defensive: every non-list/inspect op should broadcast. Catch
        # drift in the op-set definitions.
        error = ErrorResponse(detail=f"Op {op!r} has no broadcast handler")
        return JSONResponse(content=error.model_dump(), status_code=500)

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

    layout_mutex: LayoutMutex = request.app.state.layout_mutex
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
            return JSONResponse(content=error_body, status_code=409)
        try:
            broadcaster: WebSocketBroadcaster = request.app.state.broadcaster
            broadcaster.broadcast_layout_op(op, args_raw, requester_agent_id=agent_id)
        finally:
            layout_mutex.release(agent_id, op)
    else:
        broadcaster = request.app.state.broadcaster
        broadcaster.broadcast_layout_op(op, args_raw, requester_agent_id=agent_id)

    logger.info("layout op={} agent_id={} args={}", op, agent_id, args_raw)
    response_body: dict[str, Any] = {"ok": True}
    if allocated_ref is not None:
        response_body["ref"] = allocated_ref
    return JSONResponse(content=response_body)


def _inject_agent_id_meta_tag(html_content: str) -> str:
    """Inject the primary agent ID as a meta tag for the frontend."""
    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    meta_tag = f'<meta name="system-interface-agent-id" content="{agent_id}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")


def create_application(
    config: Config | None = None,
    provider_names: tuple[str, ...] | None = None,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
    agent_manager: AgentManager | None = None,
    claude_auth_service: ClaudeAuthService | None = None,
    welcome_resender: WelcomeResender | None = None,
) -> FastAPI:
    application = FastAPI(lifespan=_lifespan)

    @application.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        logger.error("Unhandled exception on {} {}: {}\n{}", request.method, request.url.path, exc, "".join(tb))
        return JSONResponse(
            status_code=500,
            content={"detail": f"Internal server error: {exc}"},
        )

    application.state.preconfigured_agent_manager = agent_manager
    application.state.config = config or Config()
    application.state.provider_names = provider_names
    application.state.include_filters = include_filters
    application.state.exclude_filters = exclude_filters
    # One long-lived ClaudeAuthService per app so the in-flight OAuth
    # subprocess survives between the /start and /submit-code requests.
    application.state.claude_auth_service = claude_auth_service or ClaudeAuthService()
    application.state.welcome_resender = welcome_resender or WelcomeResender()
    # Per-agent watcher registries. Seeded here (not only in ``_lifespan``) so the
    # attributes always exist on ``app.state`` -- ``_stop_all_watchers`` runs in test
    # paths that construct the app without driving the lifespan, and would otherwise
    # have to defensively probe for these attributes.
    application.state.watchers = {}
    application.state.tickets_watchers = {}

    plugin_manager = get_plugin_manager()
    plugin_manager.hook.endpoint(app=application)

    application.add_api_route("/", _index, methods=["GET"])
    application.add_api_route("/favicon.ico", _favicon, methods=["GET"])
    application.add_api_route("/api/agents", _list_agents_endpoint, methods=["GET"])
    application.add_api_route("/api/agents/create-worktree", _create_worktree_agent, methods=["POST"])
    application.add_api_route("/api/agents/create-chat", _create_chat_agent, methods=["POST"])
    application.add_api_route("/api/random-name", _random_name_endpoint, methods=["GET"])
    application.add_api_route("/api/agents/{agent_id}/events", _get_events, methods=["GET"])
    application.add_api_route("/api/agents/{agent_id}/stream", _stream_events, methods=["GET"])
    application.add_api_route("/api/agents/{agent_id}/message", _send_message_endpoint, methods=["POST"])
    application.add_api_route("/api/agents/{agent_id}/interrupt", _interrupt_agent_endpoint, methods=["POST"])
    application.add_api_route("/api/layout", _get_layout, methods=["GET"])
    application.add_api_route("/api/layout", _save_layout, methods=["POST"])
    application.add_api_route("/api/agents/{agent_id}/screen", _get_screen_capture, methods=["GET"])
    application.add_api_route("/api/agents/{agent_id}/destroy", _destroy_agent, methods=["POST"])
    application.add_api_route("/api/agents/{agent_id}/start", _start_agent, methods=["POST"])
    claude_auth_endpoints.register_routes(application)
    application.add_api_route("/api/layout/broadcast", _layout_broadcast_endpoint, methods=["POST"])
    application.add_api_route(
        "/api/agents/{agent_id}/subagents/{subagent_session_id}/events", _get_subagent_events, methods=["GET"]
    )
    application.add_api_route(
        "/api/agents/{agent_id}/subagents/{subagent_session_id}/stream", _stream_subagent_events, methods=["GET"]
    )
    application.add_api_websocket_route("/api/ws", _ws_endpoint)
    application.add_api_websocket_route("/api/proto-agents/{agent_id}/logs", _proto_agent_logs_endpoint)
    application.add_api_route("/plugins/{basename}", _serve_static_file, methods=["GET"])

    assets_directory = STATIC_DIRECTORY / "assets"
    if assets_directory.is_dir():
        application.mount("/assets", StaticFiles(directory=assets_directory), name="assets")

    # Service forwarding routes: /service/<name>/... forwards to the service's
    # local backend (from runtime/applications.toml) with path rewriting,
    # cookie scoping, WS shim, and a scoped service worker.
    register_service_routes(application)

    application.add_api_route("/{path:path}", _index, methods=["GET"])

    return application
