from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel


class AgentCreationError(ValueError):
    """Raised when agent creation fails due to invalid input."""

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


class SendMessageResponse(FrozenModel):
    """Response from the message endpoint."""

    status: str = Field(description="Status of the send operation")


class InterruptAgentResponse(FrozenModel):
    """Response from the /api/agents/{id}/interrupt endpoint."""

    status: str = Field(description="Status of the interrupt operation")


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
    auth_method: str | None = Field(default=None, description="e.g. 'oauth', 'api_key'")
    api_provider: str | None = Field(default=None, description="e.g. 'anthropic', 'claudeai'")
    email: str | None = Field(default=None, description="The authenticated user's email, if any")
    org_id: str | None = Field(default=None, description="Anthropic organization ID, if any")
    org_name: str | None = Field(default=None, description="Anthropic organization name, if any")
    subscription_type: str | None = Field(
        default=None, description="Subscription tier (e.g. 'Max'); absent for Console accounts"
    )


class ClaudeOAuthStartRequest(FrozenModel):
    """Request body for POST /api/claude-auth/start."""

    provider: str = Field(description="Either 'claudeai' (subscription) or 'console'")


class ClaudeOAuthStartResponse(FrozenModel):
    """Response from POST /api/claude-auth/start."""

    session_id: str = Field(description="Opaque token identifying the in-flight OAuth session")
    oauth_url: str = Field(description="URL the user opens to authorize the login")


class ClaudeOAuthSubmitCodeRequest(FrozenModel):
    """Request body for POST /api/claude-auth/submit-code."""

    session_id: str = Field(description="session_id returned by /start")
    code: str = Field(description="The CODE#STATE the user pasted from the browser")


class ClaudeAuthApiKeyRequest(FrozenModel):
    """Request body for POST /api/claude-auth/submit-api-key."""

    api_key: SecretStr = Field(description="A raw `sk-ant-...` API key")


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
