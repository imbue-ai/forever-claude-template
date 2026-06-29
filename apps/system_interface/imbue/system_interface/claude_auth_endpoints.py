"""HTTP endpoint handlers for `/api/claude-auth/*`.

Kept in a separate module from server.py so server.py doesn't grow with
the modal-specific logic. The chokepoint `_on_auth_success` is where the
paste and API-key paths converge so the welcome-resend check runs exactly
once per successful login.

The `ClaudeAuthService` (which holds the in-flight OAuth subprocess) and
the `WelcomeResender` are created once in `create_application` and stored
on the app's `SystemInterfaceState`; each handler reads them via
`get_state()` so the OAuth subprocess survives between the `/start` and
`/submit-code` calls.
"""

from __future__ import annotations

import json

from flask import Flask
from flask import Response
from flask import request
from loguru import logger as _loguru_logger

from imbue.system_interface import claude_auth
from imbue.system_interface.app_context import get_state
from imbue.system_interface.models import ClaudeAuthApiKeyRequest
from imbue.system_interface.models import ClaudeAuthStatusResponse
from imbue.system_interface.models import ClaudeOAuthStartRequest
from imbue.system_interface.models import ClaudeOAuthStartResponse
from imbue.system_interface.models import ClaudeOAuthSubmitCodeRequest
from imbue.system_interface.models import ErrorResponse
from imbue.system_interface.welcome_resend import WelcomeResender

logger = _loguru_logger


def _json_response(content: object, status_code: int = 200) -> Response:
    body = json.dumps(content, separators=(",", ":"), ensure_ascii=False)
    return Response(body, status=status_code, mimetype="application/json")


def _status_to_response(status: claude_auth.AuthStatus) -> ClaudeAuthStatusResponse:
    # Both models share the same field names and types; validating directly
    # off the AuthStatus dump keeps the conversion automatic so adding a
    # field to one side only needs the matching field added to the other,
    # not a third edit here.
    return ClaudeAuthStatusResponse.model_validate(status.model_dump())


def _error_response(detail: str, status_code: int = 400) -> Response:
    return _json_response(ErrorResponse(detail=detail).model_dump(), status_code=status_code)


def _on_auth_success(status: claude_auth.AuthStatus, welcome_resender: WelcomeResender) -> None:
    """Chokepoint for every auth-success path: run welcome-resend check.

    `WelcomeResender.check_and_resend_welcome` is itself failure-tolerant
    (logs and returns False on internal errors, including an unresolved
    target agent), so we deliberately do not wrap it in a broad
    try/except here. Anything it raises is a structural bug that should
    propagate.
    """
    if not status.logged_in:
        return
    welcome_resender.check_and_resend_welcome()


def get_status() -> Response:
    """GET /api/claude-auth/status -- current auth state."""
    service: claude_auth.ClaudeAuthService = get_state().claude_auth_service
    try:
        status = service.get_auth_status()
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=500)
    return _json_response(_status_to_response(status).model_dump())


def start_oauth() -> Response:
    """POST /api/claude-auth/start -- spawn `claude auth login --<provider>`."""
    service: claude_auth.ClaudeAuthService = get_state().claude_auth_service
    try:
        body = ClaudeOAuthStartRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    try:
        provider = claude_auth.OAuthProvider(body.provider)
    except ValueError:
        return _error_response(f"Unknown provider {body.provider!r}; must be 'claudeai' or 'console'")
    try:
        result = service.start_oauth_login(provider)
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=500)
    return _json_response(
        ClaudeOAuthStartResponse(session_id=result.session_id, oauth_url=result.oauth_url).model_dump()
    )


def submit_oauth_code() -> Response:
    """POST /api/claude-auth/submit-code -- submit user's pasted CODE#STATE."""
    state = get_state()
    service: claude_auth.ClaudeAuthService = state.claude_auth_service
    welcome_resender: WelcomeResender = state.welcome_resender
    try:
        body = ClaudeOAuthSubmitCodeRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    try:
        status = service.submit_oauth_code(body.session_id, body.code)
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=400)
    _on_auth_success(status, welcome_resender)
    return _json_response(_status_to_response(status).model_dump())


def submit_api_key() -> Response:
    """POST /api/claude-auth/submit-api-key -- persist key and restart claude agents."""
    state = get_state()
    service: claude_auth.ClaudeAuthService = state.claude_auth_service
    welcome_resender: WelcomeResender = state.welcome_resender
    try:
        body = ClaudeAuthApiKeyRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    if not body.api_key.get_secret_value().strip():
        return _error_response("api_key must be a non-empty string")
    try:
        status = service.submit_api_key(body.api_key)
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=500)
    _on_auth_success(status, welcome_resender)
    return _json_response(_status_to_response(status).model_dump())


def abort_oauth() -> Response:
    """POST /api/claude-auth/abort -- drop the in-flight OAuth subprocess."""
    service: claude_auth.ClaudeAuthService = get_state().claude_auth_service
    service.abort_oauth_login()
    return _json_response({"status": "ok"})


def register_routes(application: Flask) -> None:
    """Wire `/api/claude-auth/*` endpoints onto the Flask application.

    The handlers read the `ClaudeAuthService` / `WelcomeResender` from the
    app's `SystemInterfaceState`; `create_application` is responsible for
    placing them there before the app serves requests.
    """
    application.add_url_rule("/api/claude-auth/status", view_func=get_status, methods=["GET"])
    application.add_url_rule("/api/claude-auth/start", view_func=start_oauth, methods=["POST"])
    application.add_url_rule("/api/claude-auth/submit-code", view_func=submit_oauth_code, methods=["POST"])
    application.add_url_rule("/api/claude-auth/submit-api-key", view_func=submit_api_key, methods=["POST"])
    application.add_url_rule("/api/claude-auth/abort", view_func=abort_oauth, methods=["POST"])
