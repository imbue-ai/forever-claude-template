"""HTTP endpoint handlers for `/api/claude-auth/*`.

Kept in a separate module from server.py so server.py doesn't grow with
the modal-specific logic. The chokepoint `_on_auth_success` is where the
paste and API-key paths converge so the welcome-resend check runs exactly
once per successful login.
"""

from __future__ import annotations

from fastapi import FastAPI
from loguru import logger as _loguru_logger
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from imbue.minds_workspace_server import claude_auth
from imbue.minds_workspace_server import welcome_resend
from imbue.minds_workspace_server.models import ClaudeAuthApiKeyRequest
from imbue.minds_workspace_server.models import ClaudeAuthStatusResponse
from imbue.minds_workspace_server.models import ClaudeOAuthStartRequest
from imbue.minds_workspace_server.models import ClaudeOAuthStartResponse
from imbue.minds_workspace_server.models import ClaudeOAuthSubmitCodeRequest
from imbue.minds_workspace_server.models import ErrorResponse

logger = _loguru_logger


def _status_to_response(status: claude_auth.AuthStatus) -> ClaudeAuthStatusResponse:
    # Both models share the same field names and types; validating directly
    # off the AuthStatus dump keeps the conversion automatic so adding a
    # field to one side only needs the matching field added to the other,
    # not a third edit here.
    return ClaudeAuthStatusResponse.model_validate(status.model_dump())


def _error_response(detail: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(content=ErrorResponse(detail=detail).model_dump(), status_code=status_code)


async def _on_auth_success(status: claude_auth.AuthStatus, chat_agent_name: str | None) -> None:
    """Chokepoint for every auth-success path: run welcome-resend check.

    `welcome_resend.check_and_resend_welcome` is itself failure-tolerant
    (logs and returns False on internal errors), so we deliberately do
    not wrap it in a broad try/except here. Anything it raises is a
    structural bug that should propagate.
    """
    if not status.logged_in or not chat_agent_name:
        return
    await run_in_threadpool(welcome_resend.check_and_resend_welcome, chat_agent_name)


async def get_status(request: Request) -> JSONResponse:
    """GET /api/claude-auth/status — current auth state."""
    try:
        status = await run_in_threadpool(claude_auth.get_auth_status)
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=500)
    return JSONResponse(content=_status_to_response(status).model_dump())


async def start_oauth(request: Request) -> JSONResponse:
    """POST /api/claude-auth/start — spawn `claude auth login --<provider>`."""
    try:
        body = ClaudeOAuthStartRequest.model_validate(await request.json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    try:
        provider = claude_auth.OAuthProvider(body.provider)
    except ValueError:
        return _error_response(
            f"Unknown provider {body.provider!r}; must be 'claudeai' or 'console'"
        )
    try:
        result = await run_in_threadpool(claude_auth.start_oauth_login, provider)
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=500)
    return JSONResponse(
        content=ClaudeOAuthStartResponse(
            session_id=result.session_id, oauth_url=result.oauth_url
        ).model_dump()
    )


async def submit_oauth_code(request: Request) -> JSONResponse:
    """POST /api/claude-auth/submit-code — submit user's pasted CODE#STATE."""
    try:
        body = ClaudeOAuthSubmitCodeRequest.model_validate(await request.json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    try:
        status = await run_in_threadpool(claude_auth.submit_oauth_code, body.session_id, body.code)
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=400)
    await _on_auth_success(status, body.chat_agent_name)
    return JSONResponse(content=_status_to_response(status).model_dump())


async def submit_api_key(request: Request) -> JSONResponse:
    """POST /api/claude-auth/submit-api-key — persist key and restart claude agents."""
    try:
        body = ClaudeAuthApiKeyRequest.model_validate(await request.json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    if not body.api_key.get_secret_value().strip():
        return _error_response("api_key must be a non-empty string")
    try:
        status = await run_in_threadpool(claude_auth.submit_api_key, body.api_key)
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=500)
    await _on_auth_success(status, body.chat_agent_name)
    return JSONResponse(content=_status_to_response(status).model_dump())


async def abort_oauth(request: Request) -> JSONResponse:
    """POST /api/claude-auth/abort — drop the in-flight OAuth subprocess."""
    await run_in_threadpool(claude_auth.abort_oauth_login)
    return JSONResponse(content={"status": "ok"})


def register_routes(application: FastAPI) -> None:
    """Wire `/api/claude-auth/*` endpoints onto the FastAPI application."""
    application.add_api_route("/api/claude-auth/status", get_status, methods=["GET"])
    application.add_api_route("/api/claude-auth/start", start_oauth, methods=["POST"])
    application.add_api_route("/api/claude-auth/submit-code", submit_oauth_code, methods=["POST"])
    application.add_api_route("/api/claude-auth/submit-api-key", submit_api_key, methods=["POST"])
    application.add_api_route("/api/claude-auth/abort", abort_oauth, methods=["POST"])
