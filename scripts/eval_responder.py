"""Eval worker (supervisord one-shot). Drives a multi-turn conversation from the case's `prompts`
array, snapshots /mngr to S3 per turn (restic), and uploads the transcript at the end -- so a
launched run completes on its own and everything is retrievable from S3 without the launching
machine staying on.

Eval mode is gated on scripts/test_case_metadata.json; absent -> immediate no-op (normal workspaces).

Each entry in config["prompts"] is one turn's user message. A literal string is sent verbatim; the
sentinel DECIDE_FROM_PERSONA makes the worker role-play the client (transcript-so-far + persona ->
Anthropic API, via eval_decider).

Turn logic (N = len(prompts)):
  turn 1        -> send prompts[0]  (always a literal -- the opening ask)
  turns 2 .. N  -> restic snapshot post_message_<turn-1>, then send prompts[turn-1]
  after N       -> wait for the final agent reply, upload transcript, mark finished, exit
Each turn writes state.json (waits_done / num_turns / ongoing|finished).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import eval_decider
import eval_wait_watcher as watcher

CONFIG_PATH = Path("scripts/test_case_metadata.json")
DONE_MARKER = Path("runtime/eval_done")
OVERALL_TIMEOUT_SECONDS = 3 * 3600.0  # matches the 3h sandbox cap
DECIDE_SENTINEL = "DECIDE_FROM_PERSONA"


def _load_config() -> dict | None:
    if not CONFIG_PATH.is_file():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (ValueError, OSError):
        return None


def _resolve_message(prompt: str, agent_id: str, config: dict) -> str:
    """A literal prompt is sent as-is; DECIDE_FROM_PERSONA is role-played from the transcript so far."""
    if prompt == DECIDE_SENTINEL:
        return eval_decider.decide_next_message(
            agent_id, config.get("persona", ""), config.get("anthropic_api_key", ""))
    return prompt


def main() -> None:
    config = _load_config()
    if config is None:
        print("[eval] no scripts/test_case_metadata.json -- not eval mode, exiting", flush=True)
        return
    if DONE_MARKER.exists():
        print("[eval] already finished (marker present) -- exiting", flush=True)
        return

    from eval_aws_sink import AwsSink

    # Creds come from test_case_metadata.json (see eval_aws_sink); we drive restic ourselves. backup_provider is
    # configure_later, so host-backup is already idle -- nothing to stop.
    sink = AwsSink(config)

    deadline = time.time() + OVERALL_TIMEOUT_SECONDS
    agent_id = watcher.resolve_chat_agent_id(deadline)
    if agent_id is None:
        print("[eval] could not resolve chat agent id -- exiting", flush=True)
        return

    prompts = config.get("prompts") or []
    num_turns = len(prompts)
    if num_turns == 0:
        print("[eval] no prompts in config -- nothing to do, exiting", flush=True)
        return
    sink.write_state(0, num_turns, "ongoing")

    for turn, prompt in enumerate(prompts, start=1):
        if not watcher.wait_until(agent_id, waiting=True, deadline=deadline):
            print("[eval] timed out before turn {} -- leaving ongoing".format(turn), flush=True)
            sink.write_state(turn - 1, num_turns, "ongoing")
            return
        if turn > 1:
            sink.restic_snapshot("post_message_{}".format(turn - 1))
        watcher.send_message(agent_id, _resolve_message(prompt, agent_id, config), deadline)
        sink.write_state(turn, num_turns, "ongoing")
        watcher.wait_until(agent_id, waiting=False, deadline=deadline)

    # All prompts sent; wait for the agent's final reply, then upload + finish.
    if not watcher.wait_until(agent_id, waiting=True, deadline=deadline):
        print("[eval] timed out before final response -- leaving ongoing", flush=True)
        sink.write_state(num_turns, num_turns, "ongoing")
        return
    sink.upload_transcript(watcher.fetch_all_events(agent_id))
    sink.write_state(num_turns, num_turns, "finished")
    DONE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    DONE_MARKER.write_text("")
    print("[eval] finished after {} turns".format(num_turns), flush=True)


if __name__ == "__main__":
    main()
