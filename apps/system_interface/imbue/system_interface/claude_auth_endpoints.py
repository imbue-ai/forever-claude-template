"""HTTP endpoint handlers for `/api/claude-auth/*`.

Kept in a separate module from server.py so server.py doesn't grow with
the modal-specific logic. Every successful auth path hands the welcome
resender's `check_and_resend_welcome` to the service as the completion
hook, so the welcome-resend check runs exactly once per successful login
-- after the restarted chat agent is back up (or inline on the no-restart
subscription fast path).

The `ClaudeAuthService` (which holds the in-flight setup-token subprocess)
and the `WelcomeResender` are created once in `create_application` and
stored on the app's `SystemInterfaceState`; each handler reads them via
`get_state()` so the subprocess survives between the `/setup-token/start`
call and the subsequent `/setup-token/poll` / `/setup-token/submit-code`
calls.
"""

from __future__ import annotations

import json

from flask import Flask
from flask import Response
from flask import request
from loguru import logger as _loguru_logger

from imbue.system_interface import claude_auth
from imbue.system_interface.app_context import get_state
from imbue.system_interface.models import ClaudeAuthCredentialsRequest
from imbue.system_interface.models import ClaudeAuthStatusResponse
from imbue.system_interface.models import ClaudeOAuthLoginStartRequest
from imbue.system_interface.models import ClaudeSetupTokenPollRequest
from imbue.system_interface.models import ClaudeSetupTokenPollResponse
from imbue.system_interface.models import ClaudeSetupTokenStartResponse
from imbue.system_interface.models import ClaudeSetupTokenSubmitCodeRequest
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
    # Every auth-flow failure funnels through here; without this log the
    # container's service log shows only the access line for the 4xx/5xx,
    # leaving no server-side trace of what actually went wrong.
    logger.warning("Returning claude-auth error response ({}): {}", status_code, detail)
    return _json_response(ErrorResponse(detail=detail).model_dump(), status_code=status_code)




def get_status() -> Response:
    """GET /api/claude-auth/status -- current auth state."""
    service: claude_auth.ClaudeAuthService = get_state().claude_auth_service
    try:
        status = service.get_auth_status()
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=500)
    return _json_response(_status_to_response(status).model_dump())


def start_setup_token() -> Response:
    """POST /api/claude-auth/setup-token/start -- spawn `claude setup-token`."""
    service: claude_auth.ClaudeAuthService = get_state().claude_auth_service
    try:
        result = service.start_setup_token()
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=500)
    return _json_response(
        ClaudeSetupTokenStartResponse(session_id=result.session_id, oauth_url=result.oauth_url).model_dump()
    )


def poll_setup_token() -> Response:
    """POST /api/claude-auth/setup-token/poll -- check for the minted token.

    The `claude setup-token` subprocess polls Anthropic itself and prints
    the token once the user approves in the browser, so the frontend just
    calls this periodically; completion writes the settings env block and
    starts the background agent restart before returning.
    """
    state = get_state()
    service: claude_auth.ClaudeAuthService = state.claude_auth_service
    welcome_resender: WelcomeResender = state.welcome_resender
    try:
        body = ClaudeSetupTokenPollRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    try:
        result = service.poll_setup_token(body.session_id, welcome_resender.check_and_resend_welcome)
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=400)
    if not result.is_complete or result.status is None:
        return _json_response(ClaudeSetupTokenPollResponse(is_complete=False).model_dump())
    return _json_response(
        ClaudeSetupTokenPollResponse(is_complete=True, status=_status_to_response(result.status)).model_dump()
    )


def submit_setup_token_code() -> Response:
    """POST /api/claude-auth/setup-token/submit-code -- paste-code fallback."""
    state = get_state()
    service: claude_auth.ClaudeAuthService = state.claude_auth_service
    welcome_resender: WelcomeResender = state.welcome_resender
    try:
        body = ClaudeSetupTokenSubmitCodeRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    try:
        status = service.submit_setup_token_code(
            body.session_id, body.code, welcome_resender.check_and_resend_welcome
        )
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=400)
    return _json_response(_status_to_response(status).model_dump())


def submit_credentials() -> Response:
    """POST /api/claude-auth/submit-credentials -- write settings env, restart agents.

    The single endpoint behind the API-key field, the Imbue blob textarea,
    and the subtle direct-token paste. The strict parse rejects unmanaged
    keys and mixed-mode pastes with a user-facing 400 before anything is
    written or restarted.
    """
    state = get_state()
    service: claude_auth.ClaudeAuthService = state.claude_auth_service
    welcome_resender: WelcomeResender = state.welcome_resender
    try:
        body = ClaudeAuthCredentialsRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    if not body.credentials.get_secret_value().strip():
        return _error_response("credentials must be a non-empty string")
    try:
        status = service.submit_credentials(
            body.credentials.get_secret_value(), welcome_resender.check_and_resend_welcome
        )
    except claude_auth.CredentialPasteError as e:
        return _error_response(str(e), status_code=400)
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=500)
    return _json_response(_status_to_response(status).model_dump())


def start_oauth_login() -> Response:
    """POST /api/claude-auth/oauth/start -- spawn `claude auth login --<provider>`."""
    service: claude_auth.ClaudeAuthService = get_state().claude_auth_service
    try:
        body = ClaudeOAuthLoginStartRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    try:
        provider = claude_auth.OAuthProvider(body.provider)
    except ValueError:
        return _error_response(f"Unknown provider: {body.provider!r}")
    try:
        result = service.start_oauth_login(provider)
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=500)
    return _json_response(
        ClaudeSetupTokenStartResponse(session_id=result.session_id, oauth_url=result.oauth_url).model_dump()
    )


def poll_oauth_login() -> Response:
    """POST /api/claude-auth/oauth/poll -- check for browser sign-in completion.

    On completion the fast path (subscription, empty managed env) returns a
    plain signed-in status with no restart fields; the switching cases and
    Console return a status whose restart_* fields drive the checklist.
    """
    state = get_state()
    service: claude_auth.ClaudeAuthService = state.claude_auth_service
    welcome_resender: WelcomeResender = state.welcome_resender
    try:
        body = ClaudeSetupTokenPollRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    try:
        result = service.poll_oauth_login(body.session_id, welcome_resender.check_and_resend_welcome)
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=400)
    if not result.is_complete or result.status is None:
        return _json_response(ClaudeSetupTokenPollResponse(is_complete=False).model_dump())
    return _json_response(
        ClaudeSetupTokenPollResponse(is_complete=True, status=_status_to_response(result.status)).model_dump()
    )


def submit_oauth_login_code() -> Response:
    """POST /api/claude-auth/oauth/submit-code -- paste-code path for browser sign-in."""
    state = get_state()
    service: claude_auth.ClaudeAuthService = state.claude_auth_service
    welcome_resender: WelcomeResender = state.welcome_resender
    try:
        body = ClaudeSetupTokenSubmitCodeRequest.model_validate(request.get_json())
    except (ValueError, TypeError) as e:
        return _error_response(f"Invalid request body: {e}")
    try:
        status = service.submit_oauth_login_code(
            body.session_id, body.code, welcome_resender.check_and_resend_welcome
        )
    except claude_auth.ClaudeAuthError as e:
        return _error_response(str(e), status_code=400)
    return _json_response(_status_to_response(status).model_dump())


def abort_setup_token() -> Response:
    """POST /api/claude-auth/abort -- drop the in-flight setup-token subprocess."""
    service: claude_auth.ClaudeAuthService = get_state().claude_auth_service
    service.abort_auth_flow()
    return _json_response({"status": "ok"})


def register_routes(application: Flask) -> None:
    """Wire `/api/claude-auth/*` endpoints onto the Flask application.

    The handlers read the `ClaudeAuthService` / `WelcomeResender` from the
    app's `SystemInterfaceState`; `create_application` is responsible for
    placing them there before the app serves requests.
    """
    application.add_url_rule("/api/claude-auth/status", view_func=get_status, methods=["GET"])
    application.add_url_rule("/api/claude-auth/setup-token/start", view_func=start_setup_token, methods=["POST"])
    application.add_url_rule("/api/claude-auth/setup-token/poll", view_func=poll_setup_token, methods=["POST"])
    application.add_url_rule(
        "/api/claude-auth/setup-token/submit-code", view_func=submit_setup_token_code, methods=["POST"]
    )
    application.add_url_rule("/api/claude-auth/submit-credentials", view_func=submit_credentials, methods=["POST"])
    application.add_url_rule("/api/claude-auth/oauth/start", view_func=start_oauth_login, methods=["POST"])
    application.add_url_rule("/api/claude-auth/oauth/poll", view_func=poll_oauth_login, methods=["POST"])
    application.add_url_rule("/api/claude-auth/oauth/submit-code", view_func=submit_oauth_login_code, methods=["POST"])
    application.add_url_rule("/api/claude-auth/abort", view_func=abort_setup_token, methods=["POST"])
