"""Auto-revive dead chat agents and surface the restart in their chat.

A chat agent's claude process can die without the user asking for it: an
out-of-memory shed (earlyoom SIGTERMs the pid), a crash, or a whole-container
restart. Before this module, a dead chat just sat there -- the UI kept
rendering its last transcript message (often a stale API error) with no sign
anything was wrong, until the user happened to send a message (the messenger
auto-starts dead agents on send).

The ``ChatReviver`` closes that gap. The ``AgentManager`` feeds it every
agent-state snapshot (initial discovery plus each observe event); when a
managed chat is seen in a dead lifecycle state, the reviver schedules a
revival and, when due, delivers a restart notice through the manager's normal
message path. That single send both relaunches the agent (mngr revives DONE
husks and starts STOPPED agents on message delivery) and produces a visible
turn in the chat -- the agent acknowledges the restart, so the user is never
left staring at pre-crash history.

Which dead states revive which chats:

- ``DONE`` -- the claude process exited but its tmux session lingers. That
  only happens when the process died out from under the agent (crash, OOM
  shed, ctrl-c in the pane), so *any* managed chat found DONE is revived.
- ``STOPPED`` -- no tmux session at all. This is ambiguous: it is the state
  after a container reboot, but also after an explicit ``mngr stop``. Only
  the initial chat agent (the mind's primary chat, whose id the bootstrap
  persists) is revived from STOPPED: the mind should always be up, while a
  deliberately-stopped secondary chat stays down until messaged.

Safety valves, because revival costs memory and an LLM turn:

- **Memory guard**: revival is deferred while the host's available memory is
  below a floor just above earlyoom's SIGTERM threshold -- reviving into
  memory pressure would just get the chat shed again.
- **Backoff**: consecutive revivals of the same agent back off exponentially,
  so a chat that dies immediately after every revival (e.g. sustained memory
  pressure) cannot thrash. The attempt counter resets once the chat has
  stayed up for a stability window.

All side-effecting collaborators (message send, memory probe, clock) are
injected so the policy is unit-testable without threads, mngr, or /proc.
"""

import threading
import time
from collections.abc import Callable
from collections.abc import Iterable
from pathlib import Path

from loguru import logger as _loguru_logger

from imbue.mngr.errors import MngrError
from imbue.system_interface.models import AgentStateItem

logger = _loguru_logger

# Lifecycle states (AgentLifecycleState.value strings) in which the agent's
# main process is alive. Anything else is dead or unknowable.
_ALIVE_STATES = frozenset(("RUNNING", "WAITING", "REPLACED", "RUNNING_UNKNOWN_AGENT_TYPE"))
# The claude process exited but tmux still holds the session: unambiguously a
# crash/kill, never the result of a clean stop or reboot.
_DONE_STATE = "DONE"
# No tmux session: a reboot or an explicit stop (indistinguishable).
_STOPPED_STATE = "STOPPED"

# Consecutive-revival backoff: delay before attempt N is _REVIVE_DELAYS[N]
# (clamped to the last entry). The first revival after a fresh death fires
# almost immediately; a chat that keeps dying waits longer each round.
_REVIVE_DELAYS_SECONDS = (5.0, 60.0, 300.0, 900.0, 1800.0)
# A chat alive this long after its last revival attempt is considered stable
# again: its attempt counter resets so a much-later death revives promptly.
_STABILITY_RESET_SECONDS = 600.0
# Defer revival while available memory is below this fraction of total.
# earlyoom SIGTERMs at 10% -- reviving below ~13% would feed the new process
# straight back to it.
_MIN_MEMORY_AVAILABLE_FRACTION = 0.13
# How long to wait before re-checking when the memory guard defers a revival.
_MEMORY_DEFER_SECONDS = 120.0
# Upper bound on the worker's condition wait, so shutdown is always prompt.
_MAX_WAIT_SECONDS = 60.0

_PROC_MEMINFO_PATH = Path("/proc/meminfo")

# Sent through the normal message path; the delivery itself relaunches the
# agent, and the agent's reply is the user-visible indication of the restart.
RESTART_NOTICE_MESSAGE = (
    "[Automatic recovery notice] Your process was just restarted after it "
    "stopped unexpectedly (a crash, an out-of-memory kill, or a workspace "
    "restart). Briefly let the user know you are back. Any in-flight work was "
    "interrupted, so if you were in the middle of a task, verify its current "
    "state before continuing rather than assuming your last action completed."
)

SendMessageFn = Callable[[str, str], bool]


def read_memory_available_fraction() -> float:
    """Return MemAvailable/MemTotal from /proc/meminfo, or 1.0 when unreadable.

    Unreadable (macOS dev machines, exotic /proc layouts) means "don't guard":
    a missing probe must never permanently block revival.
    """
    try:
        meminfo_text = _PROC_MEMINFO_PATH.read_text()
    except OSError:
        return 1.0
    values_kib: dict[str, int] = {}
    for line in meminfo_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].rstrip(":") in ("MemTotal", "MemAvailable") and parts[1].isdigit():
            values_kib[parts[0].rstrip(":")] = int(parts[1])
    total_kib = values_kib.get("MemTotal", 0)
    available_kib = values_kib.get("MemAvailable")
    if total_kib <= 0 or available_kib is None:
        return 1.0
    return available_kib / total_kib


def _is_managed_chat(agent: AgentStateItem) -> bool:
    """A chat agent: not an agent-created worker, not the primary services agent.

    Mirrors ``AgentManager.get_chat_agent_ids``'s filter so the reviver manages
    exactly the agents the OOM prioritizer considers chats.
    """
    return agent.labels.get("agent_created") != "true" and agent.labels.get("is_primary") != "true"


class ChatReviver:
    """Watches agent-state snapshots and revives dead chat agents.

    ``send_message`` delivers the restart notice (and thereby relaunches the
    agent); it is the manager's normal blocking send. ``initial_chat_agent_id``
    is the one chat also revived from STOPPED (None disables that special
    case). ``memory_available_fraction`` and ``clock`` are injectable probes.

    Thread model: ``on_agent_snapshot`` is called from the manager's observe
    thread and only mutates bookkeeping under the condition's lock; a single
    worker thread (started by ``start``) performs the blocking sends. Tests
    drive the policy synchronously via ``process_due_revivals`` instead of
    starting the thread.
    """

    def __init__(
        self,
        *,
        send_message: SendMessageFn,
        initial_chat_agent_id: str | None,
        memory_available_fraction: Callable[[], float] = read_memory_available_fraction,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._send_message = send_message
        self._initial_chat_agent_id = initial_chat_agent_id
        self._memory_available_fraction = memory_available_fraction
        self._clock = clock
        self._condition = threading.Condition()
        self._shutdown_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        # All maps below are keyed by agent id and guarded by ``_condition``.
        self._state_by_id: dict[str, str] = {}
        self._due_at_by_id: dict[str, float] = {}
        self._attempts_by_id: dict[str, int] = {}
        self._last_attempt_at_by_id: dict[str, float] = {}

    def start(self) -> None:
        """Start the background worker that performs due revivals."""
        self._worker_thread = threading.Thread(target=self._run_worker, name="chat-reviver", daemon=True)
        self._worker_thread.start()

    def stop(self) -> None:
        """Stop the worker thread (idempotent; safe if never started)."""
        self._shutdown_event.set()
        with self._condition:
            self._condition.notify_all()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5)
            self._worker_thread = None

    def on_agent_snapshot(self, agents: Iterable[AgentStateItem]) -> None:
        """Fold the latest agent-state view into the revival schedule.

        Alive chats cancel their pending revival (something else brought them
        back) and, after a stability window, reset their backoff. Dead chats
        get a revival scheduled if one is not already pending. Agents absent
        from the snapshot (destroyed) are forgotten entirely.
        """
        now = self._clock()
        with self._condition:
            seen_ids: set[str] = set()
            for agent in agents:
                if not _is_managed_chat(agent):
                    continue
                seen_ids.add(agent.id)
                self._state_by_id[agent.id] = agent.state
                if agent.state in _ALIVE_STATES:
                    self._due_at_by_id.pop(agent.id, None)
                    last_attempt_at = self._last_attempt_at_by_id.get(agent.id)
                    if last_attempt_at is not None and now - last_attempt_at >= _STABILITY_RESET_SECONDS:
                        self._attempts_by_id.pop(agent.id, None)
                        self._last_attempt_at_by_id.pop(agent.id, None)
                elif self._is_revivable_dead_state(agent.id, agent.state):
                    if agent.id not in self._due_at_by_id:
                        attempts = self._attempts_by_id.get(agent.id, 0)
                        self._due_at_by_id[agent.id] = now + self._delay_for_attempt(attempts)
                else:
                    # Dead but not revivable from here (a STOPPED secondary
                    # chat, or UNKNOWN): revive-on-message stays its way back.
                    self._due_at_by_id.pop(agent.id, None)
            for absent_id in set(self._state_by_id) - seen_ids:
                self._state_by_id.pop(absent_id, None)
                self._due_at_by_id.pop(absent_id, None)
                self._attempts_by_id.pop(absent_id, None)
                self._last_attempt_at_by_id.pop(absent_id, None)
            self._condition.notify_all()

    def process_due_revivals(self) -> list[str]:
        """Attempt every revival that is due now; return the ids attempted.

        The worker thread calls this in its loop; tests call it directly for
        deterministic, threadless coverage of the policy.
        """
        attempted_ids: list[str] = []
        due_id = self._claim_next_due()
        while due_id is not None:
            attempted_ids.append(due_id)
            self._revive(due_id)
            due_id = self._claim_next_due()
        return attempted_ids

    def seconds_until_next_due(self) -> float | None:
        """Seconds until the earliest scheduled revival, or None when idle."""
        with self._condition:
            if not self._due_at_by_id:
                return None
            return max(0.0, min(self._due_at_by_id.values()) - self._clock())

    def _is_revivable_dead_state(self, agent_id: str, state: str) -> bool:
        if state == _DONE_STATE:
            return True
        return state == _STOPPED_STATE and agent_id == self._initial_chat_agent_id

    def _delay_for_attempt(self, attempts: int) -> float:
        index = min(attempts, len(_REVIVE_DELAYS_SECONDS) - 1)
        return _REVIVE_DELAYS_SECONDS[index]

    def _claim_next_due(self) -> str | None:
        """Pop the id of one due revival, re-checking its state is still dead."""
        now = self._clock()
        with self._condition:
            for agent_id, due_at in sorted(self._due_at_by_id.items(), key=lambda item: item[1]):
                if due_at > now:
                    continue
                del self._due_at_by_id[agent_id]
                state = self._state_by_id.get(agent_id)
                if state is None or not self._is_revivable_dead_state(agent_id, state):
                    continue
                return agent_id
        return None

    def _revive(self, agent_id: str) -> None:
        """Send the restart notice (which relaunches the agent), with guards.

        A memory-guard deferral reschedules without consuming an attempt; a
        failed or raising send consumes one and reschedules with backoff. On
        success nothing is rescheduled -- the next death starts a new cycle.
        """
        if self._memory_available_fraction() < _MIN_MEMORY_AVAILABLE_FRACTION:
            logger.info("Deferring revival of chat {}: available memory below guard threshold", agent_id)
            with self._condition:
                self._due_at_by_id[agent_id] = self._clock() + _MEMORY_DEFER_SECONDS
            return

        with self._condition:
            attempts = self._attempts_by_id.get(agent_id, 0) + 1
            self._attempts_by_id[agent_id] = attempts
            self._last_attempt_at_by_id[agent_id] = self._clock()

        logger.info("Reviving dead chat agent {} (attempt {})", agent_id, attempts)
        try:
            is_sent = self._send_message(agent_id, RESTART_NOTICE_MESSAGE)
        except (MngrError, OSError) as e:
            logger.warning("Revival send for chat {} raised: {}", agent_id, e)
            is_sent = False
        if not is_sent:
            with self._condition:
                self._due_at_by_id[agent_id] = self._clock() + self._delay_for_attempt(attempts)
            logger.warning("Revival of chat {} failed; retrying with backoff", agent_id)
        else:
            logger.info("Revived chat agent {}", agent_id)

    def _run_worker(self) -> None:
        while not self._shutdown_event.is_set():
            self.process_due_revivals()
            wait_seconds = self.seconds_until_next_due()
            if wait_seconds is None or wait_seconds > _MAX_WAIT_SECONDS:
                wait_seconds = _MAX_WAIT_SECONDS
            with self._condition:
                self._condition.wait(timeout=max(wait_seconds, 0.05))
