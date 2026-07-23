"""Prompt the mind to reconcile a pending OpenHost app update, in the chat.

When OpenHost redeploys a newer app version, the entrypoint's boot check
(``scripts/openhost_template_update.py``) stages the new commit into the
workspace as a local ref and writes a pending-update marker. This prompter --
run once when the AgentManager starts, i.e. once per boot -- notices that
marker and sends the initial chat agent a message asking it to run update-self
against the local ref. That message both surfaces the update in the chat and
drives the reconcile through the mind's normal skill flow (no GitHub round
trip; the source is the container-local repo).

The message is a directive the mind acts on, not a mechanical merge: update-self
carries its own validation and approval gates, so a human stays in the loop for
anything a clean fast-forward can't resolve. The pending marker is cleared by
update-self itself (via ``openhost_template_update.py mark-reconciled``) once the
merge lands, so a still-present marker on the next boot re-prompts -- desirable
when a prior attempt was abandoned mid-flight.

Side-effecting collaborators (marker read, message send, target resolution) are
injected so the prompter is unit-testable without a real workspace or mngr.
"""

from collections.abc import Callable
from pathlib import Path

from loguru import logger as _loguru_logger

logger = _loguru_logger

# Default host-env var names the entrypoint writes (see openhost_entrypoint.sh).
_PENDING_PATH_ENV_VAR = "OPENHOST_UPDATE_PENDING_PATH"
_INCOMING_REF_ENV_VAR = "OPENHOST_TEMPLATE_INCOMING_REF"

SendMessageFn = Callable[[str, str], bool]
ResolveInitialChatFn = Callable[[], str | None]


def read_pending_version(pending_marker_path: Path) -> str | None:
    """Return the pending target SHA in the marker, or None when absent/empty."""
    try:
        text = pending_marker_path.read_text().strip()
    except OSError:
        return None
    return text or None


def build_update_message(*, target_version: str, incoming_ref: str) -> str:
    """The chat directive asking the mind to run update-self from the local ref."""
    return (
        "[OpenHost app update] This app was just updated to a new version "
        f"(`{target_version}`). The new code has been staged into this workspace "
        f"as the local git ref `{incoming_ref}`. Please run the `update-self` "
        "skill to pull it in: its OpenHost branch reconciles from that local ref "
        "(NOT from GitHub upstream). Merge it into your local edits, then let me "
        "know what changed. If anything needs a decision, ask me before applying."
    )


class TemplateUpdatePrompter:
    """Sends the initial chat agent a one-shot reconcile prompt when an update is pending.

    ``send_message`` is the manager's blocking send (also relaunches a stopped
    chat). ``resolve_initial_chat_agent_id`` returns the mind's primary chat id.
    ``pending_marker_path`` / ``incoming_ref`` default to the entrypoint's
    host-env values. A single in-process guard prevents re-sending within one
    boot; a persisted marker across boots re-prompts by design.
    """

    def __init__(
        self,
        *,
        send_message: SendMessageFn,
        resolve_initial_chat_agent_id: ResolveInitialChatFn,
        pending_marker_path: Path,
        incoming_ref: str,
    ) -> None:
        self._send_message = send_message
        self._resolve_initial_chat_agent_id = resolve_initial_chat_agent_id
        self._pending_marker_path = pending_marker_path
        self._incoming_ref = incoming_ref
        self._prompted = False

    def check_and_prompt(self) -> bool:
        """If an update is pending, message the mind to run update-self. Once per boot.

        Returns True when a prompt was dispatched, False otherwise (no pending
        update, already prompted this boot, target chat unresolved, or send
        failed). Failure-tolerant: a bad send logs and returns False.
        """
        if self._prompted:
            return False
        target_version = read_pending_version(self._pending_marker_path)
        if target_version is None:
            return False
        agent_id = self._resolve_initial_chat_agent_id()
        if agent_id is None:
            logger.warning("Template update pending but no initial chat agent resolved; skipping prompt")
            return False

        self._prompted = True
        message = build_update_message(target_version=target_version, incoming_ref=self._incoming_ref)
        sent = self._send_message(agent_id, message)
        if not sent:
            logger.warning("Failed to dispatch template-update prompt to {}", agent_id)
            # Allow a later retry this boot (e.g. once the chat is reachable).
            self._prompted = False
            return False
        logger.info("Prompted {} to reconcile OpenHost app update {}", agent_id, target_version)
        return True
