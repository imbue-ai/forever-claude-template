"""Eval worker (supervisord one-shot). Drives a fixed multi-turn conversation, snapshots /mngr to
S3 per turn (restic), and uploads the transcript at the end -- so a launched run completes on its
own and everything is retrievable from S3 without the launching machine staying on.

Eval mode is gated on scripts/config.json; absent -> immediate no-op (normal workspaces).

Turn logic (N = config.num_turns; e.g. N=4):
  wait 1                 -> send config.first_prompt
  waits 2 .. N-1         -> restic snapshot post_message_<wait-1>, then send "OKAY"
  wait N (final)         -> upload transcript, mark finished, exit
Each wait writes state.json (waits_done / num_turns / ongoing|finished).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import eval_wait_watcher as watcher

CONFIG_PATH = Path("scripts/config.json")
DONE_MARKER = Path("runtime/eval_done")
OVERALL_TIMEOUT_SECONDS = 3 * 3600.0  # matches the 3h sandbox cap
_TURN_MESSAGE = "OKAY"


def _load_config() -> dict | None:
    if not CONFIG_PATH.is_file():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (ValueError, OSError):
        return None


def main() -> None:
    config = _load_config()
    if config is None:
        print("[eval] no scripts/config.json -- not eval mode, exiting", flush=True)
        return
    if DONE_MARKER.exists():
        print("[eval] already finished (marker present) -- exiting", flush=True)
        return

    from eval_aws_sink import AwsSink

    # Creds come from config.json (see eval_aws_sink); we drive restic ourselves. backup_provider is
    # configure_later, so host-backup is already idle -- nothing to stop.
    sink = AwsSink(config)

    deadline = time.time() + OVERALL_TIMEOUT_SECONDS
    agent_id = watcher.resolve_chat_agent_id(deadline)
    if agent_id is None:
        print("[eval] could not resolve chat agent id -- exiting", flush=True)
        return

    num_turns = int(config.get("num_turns", 3))
    sink.write_state(0, num_turns, "ongoing")

    for turn in range(1, num_turns + 1):
        if not watcher.wait_until(agent_id, waiting=True, deadline=deadline):
            print("[eval] timed out before wait {} -- leaving ongoing".format(turn), flush=True)
            sink.write_state(turn - 1, num_turns, "ongoing")
            return

        if turn == 1:
            watcher.send_message(agent_id, config["first_prompt"], deadline)
            sink.write_state(1, num_turns, "ongoing")
        elif turn < num_turns:
            sink.restic_snapshot("post_message_{}".format(turn - 1))
            watcher.send_message(agent_id, _TURN_MESSAGE, deadline)
            sink.write_state(turn, num_turns, "ongoing")
        else:
            sink.upload_transcript(watcher.fetch_all_events(agent_id))
            sink.write_state(num_turns, num_turns, "finished")
            DONE_MARKER.parent.mkdir(parents=True, exist_ok=True)
            DONE_MARKER.write_text("")
            print("[eval] finished after {} turns".format(num_turns), flush=True)
            return

        watcher.wait_until(agent_id, waiting=False, deadline=deadline)


if __name__ == "__main__":
    main()
