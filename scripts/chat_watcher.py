"""In-sandbox eval driver: multi-turn conversation + a state file the retriever reads.

Watches the workspace's chat agent and steps a small conversation, gated on the agent's idle
(WAITING) state -- exactly the calls the UI chat box makes, i.e. "as if the user typed it":

  wait #1 (idle after /welcome)      -> send the test case's first_prompt
  wait #2 (idle after that reply)    -> send "OK"           (placeholder second turn, for now)
  wait #3 (idle after that reply)    -> stop; the test is finished

Each observed WAITING increments a turn counter. Progress is written to a JSON state file OUTSIDE
the agent's repo (under MNGR_HOST_DIR, i.e. /mngr) so the agent never touches it:

  {"waits_processed_count": <int>, "test_state": "ongoing" | "finished"}

`retrieve-test-results` reads that file to know whether a case is done. No LLM, no external
controller: everything is loopback to the local system_interface at 127.0.0.1:8000.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

_SYSTEM_INTERFACE = "http://127.0.0.1:8000"
# Paths are relative to /mngr/code (the cwd supervisord runs services in).
_CONFIG_PATH = Path("scripts/first_command.json")
_CHAT_AGENT_ID_FILENAME = "initial_chat_agent_id"
# State file lives under MNGR_HOST_DIR (/mngr), one level above the agent's repo (/mngr/code),
# so the agent can't accidentally edit/commit it. retrieve-test-results reads this exact path.
_STATE_PATH = Path(os.environ.get("MNGR_HOST_DIR", "/mngr")) / "eval_state.json"

_TOTAL_TURNS = 3
_OVERALL_TIMEOUT_SECONDS = 3600.0
_POLL_INTERVAL_SECONDS = 3.0


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_message(agent_id: str, message: str) -> int:
    body = json.dumps({"message": message}).encode("utf-8")
    request = urllib.request.Request(
        "{}/api/agents/{}/message".format(_SYSTEM_INTERFACE, agent_id),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status


def _read_first_prompt() -> str | None:
    if not _CONFIG_PATH.is_file():
        return None
    try:
        data = json.loads(_CONFIG_PATH.read_text())
    except (ValueError, OSError):
        return None
    prompt = str(data.get("first_prompt", "")).strip()
    return prompt or None


def _write_state(waits_processed_count: int, test_state: str) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"waits_processed_count": waits_processed_count, "test_state": test_state}
    _STATE_PATH.write_text(json.dumps(payload, indent=2))


def _already_finished() -> bool:
    try:
        return json.loads(_STATE_PATH.read_text()).get("test_state") == "finished"
    except (ValueError, OSError):
        return False


def _resolve_chat_agent_id(deadline: float) -> str | None:
    host_dir = os.environ.get("MNGR_HOST_DIR", "")
    id_path = Path(host_dir) / _CHAT_AGENT_ID_FILENAME if host_dir else None
    while time.time() < deadline:
        if id_path is not None and id_path.is_file():
            agent_id = id_path.read_text().strip()
            if agent_id:
                return agent_id
        time.sleep(_POLL_INTERVAL_SECONDS)
    return None


def _agent_state(agent_id: str) -> str | None:
    try:
        agents = _get_json("{}/api/agents".format(_SYSTEM_INTERFACE)).get("agents", [])
    except (urllib.error.URLError, OSError, ValueError):
        return None
    for agent in agents:
        if agent.get("id") == agent_id:
            return (agent.get("state") or "").upper()
    return None


def _wait_until(agent_id: str, *, waiting: bool, deadline: float) -> bool:
    """Block until the agent is WAITING (waiting=True) or has left WAITING (waiting=False)."""
    while time.time() < deadline:
        state = _agent_state(agent_id)
        if state is not None and (state == "WAITING") == waiting:
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return False


def _send_with_retry(agent_id: str, message: str, deadline: float) -> bool:
    while time.time() < deadline:
        try:
            if _post_message(agent_id, message) == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(_POLL_INTERVAL_SECONDS)
    return False


def _turn_action(turn: int, first_prompt: str, total: int = _TOTAL_TURNS) -> tuple[str | None, bool]:
    """(message_to_send_or_None, is_final) for a 1-based WAITING turn.

    turn 1 -> the test case's first_prompt; turn 2 -> "OK" (placeholder second turn);
    the final turn (>= total) sends nothing and ends the test.
    """
    if turn >= total:
        return None, True
    return {1: first_prompt, 2: "OK"}.get(turn), False


def main() -> None:
    prompt = _read_first_prompt()
    if prompt is None:
        print("[chat-watcher] no first_command.json / first_prompt -- nothing to do")
        return
    if _already_finished():
        print("[chat-watcher] test already finished (state file present) -- skipping")
        return

    deadline = time.time() + _OVERALL_TIMEOUT_SECONDS
    agent_id = _resolve_chat_agent_id(deadline)
    if agent_id is None:
        print("[chat-watcher] could not resolve chat agent id within timeout -- exiting")
        return

    for turn in range(1, _TOTAL_TURNS + 1):
        if not _wait_until(agent_id, waiting=True, deadline=deadline):
            print("[chat-watcher] timed out before wait #{} -- leaving state ongoing".format(turn))
            _write_state(turn - 1, "ongoing")
            return

        message, is_final = _turn_action(turn, prompt)
        if is_final:
            _write_state(turn, "finished")
            print("[chat-watcher] finished after {} waits".format(turn))
            return

        _write_state(turn, "ongoing")
        if message is not None:
            if not _send_with_retry(agent_id, message, deadline):
                print("[chat-watcher] failed to deliver message on turn {} -- exiting".format(turn))
                return
            print("[chat-watcher] wait #{}: delivered {!r}".format(turn, message[:60]))
        # Let the agent leave WAITING (start processing) so the next wait is a fresh turn.
        _wait_until(agent_id, waiting=False, deadline=deadline)


def _self_check() -> None:
    import tempfile

    assert _turn_action(1, "hi") == ("hi", False)
    assert _turn_action(2, "hi") == ("OK", False)
    assert _turn_action(3, "hi") == (None, True)
    global _STATE_PATH
    _STATE_PATH = Path(tempfile.mkdtemp()) / "eval_state.json"
    _write_state(2, "ongoing")
    assert json.loads(_STATE_PATH.read_text()) == {"waits_processed_count": 2, "test_state": "ongoing"}
    print("self-check OK")


if __name__ == "__main__":
    import sys

    _self_check() if "--self-check" in sys.argv else main()
