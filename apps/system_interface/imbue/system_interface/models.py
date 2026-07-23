from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel


class AgentCreationError(ValueError):
    """Raised when agent creation fails due to invalid input."""

    ...


class AttachmentError(ValueError):
    """Raised when a chat attachment cannot be stored or located."""

    ...


class AgentListItem(FrozenModel):
    """An agent entry in the agent list response."""

    id: str = Field(description="The agent's unique identifier")
    name: str = Field(description="The agent's human-readable name")
    state: str = Field(description="The agent's lifecycle state")


class AgentListResponse(FrozenModel):
    """Response from the /api/agents endpoint."""

    agents: list[AgentListItem] = Field(description="List of discovered agents")


class SendMessageRequest(FrozenModel):
    """Request body for sending a message to an agent."""

    message: str = Field(description="The message text to send")
    client_id: str = Field(default="", description="Per-browser client id of the sender ('' for legacy callers)")
    active_layout: str = Field(default="", description="The sender's active layout slug at send time")
    device_kind: str = Field(default="", description="'mobile' or 'desktop', derived from the sender's user agent")


class SendMessageResponse(FrozenModel):
    """Response from the message endpoint."""

    status: str = Field(description="Status of the send operation")


class AttachmentUploadResponse(FrozenModel):
    """Response from the chat attachment upload endpoint."""

    path: str = Field(description="Absolute path to the stored upload on the agent VM")
    size: int = Field(description="Size of the stored upload in bytes")


class InterruptAgentResponse(FrozenModel):
    """Response from the /api/agents/{id}/interrupt endpoint."""

    status: str = Field(description="Status of the interrupt operation")


class ActivityRequest(FrozenModel):
    """Request body for the /api/activity endpoint.

    A snapshot of the workspace UI's current agent-tab activity, posted by the
    frontend whenever a tab opens/closes, the visible tab changes, or a message
    is sent. The backend uses it to re-tag chat agents' OOM priority. ``open`` and
    ``visible`` are the full current sets (replaced wholesale); ``messaged`` is
    set only when the report was triggered by a send, to bump that chat's recency.
    """

    open: list[str] = Field(default_factory=list, description="Agent ids of all currently open tabs")
    visible: list[str] = Field(default_factory=list, description="Agent ids of the currently visible tabs")
    messaged: str | None = Field(default=None, description="Agent id just messaged, if this report was a send")


class ActivityResponse(FrozenModel):
    """Response from the /api/activity endpoint."""

    status: str = Field(description="Status of the activity report")


class ErrorResponse(FrozenModel):
    """Error response body."""

    detail: str = Field(description="Human-readable error description")


class AgentStateItem(FrozenModel):
    """Agent state for the unified WebSocket stream."""

    id: str = Field(description="The agent's unique identifier")
    name: str = Field(description="The agent's human-readable name")
    state: str = Field(description="The agent's lifecycle state")
    labels: dict[str, str] = Field(description="Agent labels (e.g., user_created, chat_parent_id)")
    work_dir: str | None = Field(description="The agent's working directory path")
    activity_state: str | None = Field(
        default=None,
        description=(
            "Per-agent chat activity state value (THINKING / TOOL_RUNNING / "
            "IDLE), or None when no activity tracking is available for this "
            "agent."
        ),
    )


class ApplicationEntry(FrozenModel):
    """An application registered in runtime/applications.toml."""

    name: str = Field(description="Application name (e.g., 'web', 'terminal')")
    url: str = Field(description="Local URL where the application is accessible")


class TerminalSessionInfo(FrozenModel):
    """A live user-terminal tmux session (one per ad-hoc dockview terminal tab)."""

    session_name: str = Field(description="The tmux session name (e.g. 'terminal-1')")
    session_id: str = Field(description="The immutable tmux session id (e.g. '$3'), stable across rename")
    cwd: str = Field(description="The session's current working directory (tmux session_path)")


class CreateWorktreeRequest(FrozenModel):
    """Request body for creating a worktree agent."""

    name: str = Field(description="Name for the new worktree agent")
    selected_agent_id: str = Field(
        default="",
        description="ID of the agent whose work dir to create the worktree from",
    )


class CreateChatRequest(FrozenModel):
    """Request body for creating a chat agent."""

    name: str = Field(description="Name for the new chat agent")


class CreateAgentResponse(FrozenModel):
    """Response from agent creation endpoints."""

    agent_id: str = Field(description="The pre-generated agent ID")


class RandomNameResponse(FrozenModel):
    """Response from the random name endpoint."""

    name: str = Field(description="A random agent name")


class DestroyAgentResponse(FrozenModel):
    """Response from the agent destroy endpoint."""

    status: str = Field(description="Result of the destroy operation")


class StartAgentResponse(FrozenModel):
    """Response from the agent start endpoint."""

    status: str = Field(description="Result of the start operation")


class ClaudeAuthStatusResponse(FrozenModel):
    """Response from /api/claude-auth/status."""

    logged_in: bool = Field(description="Whether claude is currently authenticated")
    auth_method: str | None = Field(default=None, description="e.g. 'oauth', 'api_key', 'oauth_token'")
    api_provider: str | None = Field(default=None, description="e.g. 'anthropic', 'claudeai', 'firstParty'")
    email: str | None = Field(default=None, description="The authenticated user's email, if any")
    org_id: str | None = Field(default=None, description="Anthropic organization ID, if any")
    org_name: str | None = Field(default=None, description="Anthropic organization name, if any")
    subscription_type: str | None = Field(
        default=None, description="Subscription tier (e.g. 'Max'); absent for token/Console sessions"
    )
    auth_mode: str = Field(
        default="none",
        description="Effective auth mode: 'subscription', 'console', 'imbue', 'api_key', or 'none'. Derived from "
        "the managed settings-env keys when any are present, otherwise folded from `claude auth status`.",
    )
    masked_key_suffix: str | None = Field(
        default=None, description="Last few characters of the managed key/token, for display"
    )
    workspace_host_id: str | None = Field(
        default=None, description="This mind's mngr host id, for the desktop app's key-mint page link"
    )
    restart_phase: str | None = Field(
        default=None, description="Phase of the post-auth agent restart: 'restarting', 'finishing', 'done', 'failed'"
    )
    restart_detail: str | None = Field(default=None, description="Human-readable detail for the current restart phase")
    restart_error: str | None = Field(default=None, description="Error message when restart_phase is 'failed'")
    restart_reason: str | None = Field(
        default=None,
        description="Why the restart is running: 'credentials_saved', 'subscription_switch', 'console_switch'",
    )


class ClaudeOAuthLoginStartRequest(FrozenModel):
    """Request body for POST /api/claude-auth/oauth/start."""

    provider: str = Field(description="Which browser sign-in to run: 'claudeai' or 'console'")


class ClaudeSetupTokenStartResponse(FrozenModel):
    """Response from POST /api/claude-auth/setup-token/start."""

    session_id: str = Field(description="Opaque token identifying the in-flight setup-token session")
    oauth_url: str = Field(description="URL the user opens to authorize the login")


class ClaudeSetupTokenPollRequest(FrozenModel):
    """Request body for POST /api/claude-auth/setup-token/poll."""

    session_id: str = Field(description="session_id returned by /setup-token/start")


class ClaudeSetupTokenPollResponse(FrozenModel):
    """Response from POST /api/claude-auth/setup-token/poll."""

    is_complete: bool = Field(description="Whether the token was minted and written")
    status: ClaudeAuthStatusResponse | None = Field(
        default=None, description="Auth status after completion; None while still pending"
    )


class ClaudeSetupTokenSubmitCodeRequest(FrozenModel):
    """Request body for POST /api/claude-auth/setup-token/submit-code."""

    session_id: str = Field(description="session_id returned by /setup-token/start")
    code: str = Field(description="The CODE#STATE the user pasted from the browser")


class ClaudeAuthCredentialsRequest(FrozenModel):
    """Request body for POST /api/claude-auth/submit-credentials.

    `credentials` is env-var-style lines covering the managed auth keys:
    an `ANTHROPIC_API_KEY=...` line (optionally with `ANTHROPIC_BASE_URL=...`
    for the Imbue/LiteLLM case), or a `CLAUDE_CODE_OAUTH_TOKEN=...` line.
    """

    credentials: SecretStr = Field(description="Env-var-style credential lines (KEY=VALUE per line)")


class LatchkeyPermissionInfo(FrozenModel):
    """A grantable permission within a latchkey scope, from the gateway catalog."""

    name: str = Field(description="Permission schema name, e.g. 'slack-read-all'")
    description: str | None = Field(default=None, description="Plain-English summary of the permission")


class LatchkeyScopeInfo(FrozenModel):
    """Display info for a latchkey permission scope, from the gateway catalog.

    Returned by GET /api/latchkey/scopes/{scope}; the frontend uses
    `display_name` to label a permission-request card and the per-permission
    descriptions for hover tooltips.
    """

    scope: str = Field(description="Detent scope schema name, e.g. 'slack-api'")
    display_name: str = Field(description="Human-readable service name, e.g. 'Slack'")
    description: str | None = Field(default=None, description="Plain-English summary of the scope")
    permissions: tuple[LatchkeyPermissionInfo, ...] = Field(
        default=(), description="Permissions grantable under the scope"
    )
