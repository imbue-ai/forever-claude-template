"""Discover mngr-managed agents using the mngr Python API."""

from __future__ import annotations

import os
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.find import resolve_to_started_host_and_running_agent
from imbue.mngr.api.list import ErrorBehavior
from imbue.mngr.api.list import list_agents
from imbue.mngr.api.message import MessageResult
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.loader import load_config
from imbue.mngr.main import get_or_create_plugin_manager
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.env_utils import parse_env_file

logger = _loguru_logger


def get_host_dir() -> Path:
    """Return the mngr host directory from the environment.

    Falls back to ``~/.mngr`` when ``MNGR_HOST_DIR`` is unset. This is the
    canonical resolver shared by both the API layer (``server._find_agent``)
    and the activity-state tracker (``AgentManager``).
    """
    return Path(os.environ.get("MNGR_HOST_DIR", str(Path.home() / ".mngr")))


class AgentInfo(FrozenModel):
    """Lightweight agent info for the web UI."""

    id: str = Field(description="The agent's unique identifier")
    name: str = Field(description="The agent's human-readable name")
    state: str = Field(description="The agent's lifecycle state (e.g. RUNNING, STOPPED)")
    agent_state_dir: Path = Field(description="Path to the agent's state directory on the local host")
    claude_config_dir: Path = Field(description="Path to the Claude config directory for this agent")
    labels: dict[str, str] = Field(default_factory=dict, description="Agent labels")
    work_dir: str | None = Field(default=None, description="Agent working directory path")


def _get_mngr_context() -> tuple[MngrContext, ConcurrencyGroup]:
    cg = ConcurrencyGroup(name="system-interface")
    cg.__enter__()
    try:
        pm = get_or_create_plugin_manager()
        mngr_ctx = load_config(pm, cg, is_interactive=False)
    except BaseException:
        cg.__exit__(None, None, None)
        raise
    return mngr_ctx, cg


def _read_claude_config_dir_from_env(env_file: Path) -> Path | None:
    """Parse `env_file` and return CLAUDE_CONFIG_DIR if present, else None."""
    if not env_file.exists():
        return None
    try:
        env_vars = parse_env_file(env_file.read_text())
    except OSError:
        logger.debug("Failed to read env file: {}", env_file)
        return None
    value = env_vars.get("CLAUDE_CONFIG_DIR")
    if not value:
        return None
    return Path(value)


def read_claude_config_dir_from_env_file(agent_state_dir: Path) -> Path:
    """Resolve a Claude agent's CLAUDE_CONFIG_DIR.

    The lookup mirrors the env-resolution chain that the agent's own tmux
    session uses at startup (mngr sources the host env first, then the
    agent env), so we end up with the same answer the running agent sees:

    1. Agent's per-agent env file (`<agent_state_dir>/env`). mngr_claude
       writes `CLAUDE_CONFIG_DIR` here when `use_env_config_dir=False`,
       pinning the agent at its own per-agent config dir.
    2. Host env file (`$MNGR_HOST_DIR/env`). The bootstrap writes
       `CLAUDE_CONFIG_DIR` here when `use_env_config_dir=True` is in
       effect for the agent type, so every chat/worker/worktree agent in
       the workspace inherits the services agent's config dir.
    3. Conventional per-agent path (`<agent_state_dir>/plugin/claude/
       anthropic`) if it exists on disk. Covers legacy agents that
       predate use_env_config_dir.
    4. The user's `~/.claude` as a last-resort fallback.

    Without step 2 the session_watcher pointed at `~/.claude` for every
    use_env_config_dir=True agent, found no `projects/` subdir, and
    returned empty events to the UI -- the visible symptom was "messages
    don't show up in the chat panel" for any agent created via the
    "New Chat" button.
    """
    # 1. Per-agent env (use_env_config_dir=False)
    per_agent = _read_claude_config_dir_from_env(agent_state_dir / "env")
    if per_agent is not None:
        return per_agent
    # 2. Host env (use_env_config_dir=True; the bootstrap wrote it there)
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    if host_dir:
        host_level = _read_claude_config_dir_from_env(Path(host_dir) / "env")
        if host_level is not None:
            return host_level
    # 3. Conventional per-agent path (legacy)
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    if conventional.exists():
        return conventional
    # 4. User-level fallback
    return Path.home() / ".claude"


def discover_agents(
    provider_names: tuple[str, ...] | None = None,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> list[AgentInfo]:
    """List all mngr-managed agents."""
    mngr_ctx, cg = _get_mngr_context()
    try:
        result = list_agents(
            mngr_ctx=mngr_ctx,
            is_streaming=False,
            include_filters=include_filters,
            exclude_filters=exclude_filters,
            provider_names=provider_names,
            error_behavior=ErrorBehavior.CONTINUE,
        )
    finally:
        cg.__exit__(None, None, None)

    # Use default host dir from mngr config for local agents
    default_host_dir = mngr_ctx.config.default_host_dir

    agents: list[AgentInfo] = []
    for agent_details in result.agents:
        agent_id = str(agent_details.id)
        agent_name = str(agent_details.name)
        state = str(agent_details.state.value) if agent_details.state else "unknown"

        # Compute agent state dir from the default host dir
        agent_state_dir = default_host_dir / "agents" / agent_id

        # Get CLAUDE_CONFIG_DIR from the agent's env file
        claude_config_dir = read_claude_config_dir_from_env_file(agent_state_dir)

        agents.append(
            AgentInfo(
                id=agent_id,
                name=agent_name,
                state=state,
                agent_state_dir=agent_state_dir,
                claude_config_dir=claude_config_dir,
                labels=dict(agent_details.labels),
                work_dir=str(agent_details.work_dir),
            )
        )

    return agents


DiscoverFn = Callable[[AgentId, MngrContext], Sequence[AgentMatch]]
SendFn = Callable[[Sequence[AgentMatch], str, MngrContext], MessageResult]


def _discover_locations(agent_id: AgentId, mngr_ctx: MngrContext) -> Sequence[AgentMatch]:
    """Resolve an agent id to its location via a full mngr discovery.

    Raises ``AgentNotFoundError`` when the id matches no agent -- ``find_all_agents``
    does not return empty for an unmatched identifier.
    """
    return find_all_agents(
        addresses=(AgentAddress(agent=agent_id),),
        filter_all=False,
        target_state=None,
        mngr_ctx=mngr_ctx,
    )


def _send_to(matches: Sequence[AgentMatch], message: str, mngr_ctx: MngrContext) -> MessageResult:
    """Send a message to a pre-resolved set of agents, auto-starting STOPPED ones."""
    return send_message_to_agents(
        mngr_ctx=mngr_ctx,
        message_content=message,
        agents_to_message=matches,
        error_behavior=ErrorBehavior.CONTINUE,
        is_start_desired=True,
    )


def _send_message_to_agent(
    agent_id: AgentId,
    message: str,
    mngr_ctx: MngrContext,
    known_locations: Sequence[AgentMatch],
    *,
    discover: DiscoverFn = _discover_locations,
    send: SendFn = _send_to,
) -> bool:
    """Send to the agent with ``agent_id`` at ``known_locations``, else discovery.

    ``known_locations`` (the caller's already-resolved location, from the live
    observe cache) is messaged directly -- no discovery. On a miss, or if that send
    reaches no agent (the location just went stale: destroyed, recreated, or moved
    hosts), it falls back to a full mngr discovery. The id is globally unique, so it
    resolves to exactly the intended agent, never fanning out across same-named
    agents on other hosts.
    """
    if known_locations and send(known_locations, message, mngr_ctx).successful_agents:
        return True
    matches = discover(agent_id, mngr_ctx)
    return bool(send(matches, message, mngr_ctx).successful_agents)


def send_message(agent_id: AgentId, message: str, known_locations: Sequence[AgentMatch]) -> bool:
    """Send a message to the agent with ``agent_id``. Returns True on success.

    ``known_locations`` is the agent's already-resolved location (the caller passes
    its live observe cache) so a message skips discovery; an empty/stale value falls
    back to discovery. STOPPED agents are auto-started (`is_start_desired=True`).
    Callers go through ``AgentManager.send_message_to_agent``, which supplies the cache.
    """
    mngr_ctx, cg = _get_mngr_context()
    try:
        return _send_message_to_agent(agent_id, message, mngr_ctx, known_locations)
    finally:
        cg.__exit__(None, None, None)


def start_agent(agent_name: str) -> None:
    """Ensure an agent is running, starting it if it is STOPPED.

    This deliberately goes through the *same* in-process mngr path that
    ``send_message`` uses to auto-start a STOPPED agent: it loads the mngr
    context exactly the same way (so the same config, env, and cwd apply),
    then resolves the agent and runs mngr's own ``ensure_agent_started``
    (via ``resolve_to_started_host_and_running_agent(..., allow_auto_start=
    True)``). That is what gives us the invariant that opening an agent's
    terminal and sending it a message succeed or fail together -- neither
    reimplements the start, so neither can diverge from the other.

    ``ensure_agent_started`` is a clean no-op for an agent that is already
    running, so this is cheap in the common case (opening the terminal of an
    agent that is already up).

    Raises ``MngrError`` (e.g. ``AgentNotFoundError`` if the agent does
    not exist, or a start failure) -- callers surface these to the user.
    """
    mngr_ctx, cg = _get_mngr_context()
    try:
        address = AgentAddress(agent=AgentName(agent_name))
        host_ref, agent_ref = find_one_agent(address, mngr_ctx)
        resolve_to_started_host_and_running_agent(
            host_ref=host_ref,
            agent_ref=agent_ref,
            allow_auto_start=True,
            mngr_ctx=mngr_ctx,
        )
    finally:
        cg.__exit__(None, None, None)
