"""End-to-end: a subagent's tk steps must not leak into the main progress view.

Reproduces the real bug. A native Agent-tool subagent shares the parent's
.tickets/ dir and MNGR_AGENT_NAME, so its step records are stamped identically
to the main agent's. The main agent's transitions live in the main transcript;
the subagent's live in the subagent transcript (a different session, excluded
from the main /events stream). The fix attributes each step to a session by its
transcript signals (`Updated <id> ->` transitions, `tk create --step` titles)
and scopes the enrichment table accordingly.

This wires the real AgentSessionWatcher to the real AgentTicketsWatcher (no
fakes) over transcripts and .tickets files shaped like the production data, and
asserts the main view excludes the subagent's steps while the subagent's own
session surfaces exactly them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imbue.system_interface.session_watcher import AgentSessionWatcher
from imbue.system_interface.tickets_watcher import AgentTicketsWatcher

_AGENT_NAME = "tasteful-white-wolf"
_MAIN_SESSION = "b5a4404f-66a1-4faf-956a-190d0c4fdce8"
_SUB_SESSION = "agent-a5e0e98eb943ff033"


def _assistant_bash(uuid: str, ts: str, command: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-8",
            "content": [{"type": "tool_use", "id": f"tc-{uuid}", "name": "Bash", "input": {"command": command}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _tool_result(uuid: str, ts: str, output: str) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"tc-{uuid.replace('r-', '')}", "content": output}],
        },
    }


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _step_ticket(ticket_id: str, status: str, title: str, *, summary: str | None = None) -> str:
    summary_section = f"\n## Summary\n\n{summary}\n" if summary is not None else ""
    return (
        f"---\nid: {ticket_id}\nstatus: {status}\ndeps: []\nlinks: []\n"
        f"created: 2026-04-28T01:00:00Z\ntype: task\npriority: 2\nagent: {_AGENT_NAME}\nstep: true\n---\n"
        f"# {title}\n{summary_section}"
    )


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Lay out a main transcript, a subagent transcript, and a shared .tickets
    dir, mirroring the real repro. Returns (agent_state_dir, claude_config_dir,
    tickets_dir)."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    claude_config_dir = tmp_path / "claude_config"
    projects = claude_config_dir / "projects" / "-mngr-code"
    main_file = projects / f"{_MAIN_SESSION}.jsonl"
    sub_file = projects / _MAIN_SESSION / "subagents" / f"{_SUB_SESSION}.jsonl"

    # Main session: creates two steps, starts one, closes the other.
    _write_jsonl(
        main_file,
        [
            _assistant_bash(
                "m1", "2026-04-28T01:00:01Z", 'S1=$(tk create --step "Main work one") && S2=$(tk create --step "Main work two")'
            ),
            _tool_result("r-m1", "2026-04-28T01:00:02Z", ""),
            _assistant_bash("m2", "2026-04-28T01:00:03Z", "tk start cod-main1"),
            _tool_result("r-m2", "2026-04-28T01:00:04Z", "Updated cod-main1 -> in_progress"),
            _assistant_bash("m3", "2026-04-28T01:00:05Z", 'tk close cod-main2 "wrapped up"'),
            _tool_result("r-m3", "2026-04-28T01:00:06Z", "Updated cod-main2 -> in_progress\nUpdated cod-main2 -> closed"),
        ],
    )

    # Subagent session: creates three steps, closes one, starts one, and leaves
    # one pending (created but never started -- only attributable by title).
    _write_jsonl(
        sub_file,
        [
            _assistant_bash(
                "s1",
                "2026-04-28T01:00:10Z",
                'S1=$(tk create --step "Sub explore"); S2=$(tk create --step "Sub analyze"); S3=$(tk create --step "Sub pending step")',
            ),
            _tool_result("r-s1", "2026-04-28T01:00:11Z", ""),
            _assistant_bash("s2", "2026-04-28T01:00:12Z", 'tk close cod-sub1 "explored"'),
            _tool_result("r-s2", "2026-04-28T01:00:13Z", "Updated cod-sub1 -> in_progress\nUpdated cod-sub1 -> closed"),
            _assistant_bash("s3", "2026-04-28T01:00:14Z", "tk start cod-sub2"),
            _tool_result("r-s3", "2026-04-28T01:00:15Z", "Updated cod-sub2 -> in_progress"),
        ],
    )
    (sub_file.parent / f"{_SUB_SESSION}.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "explore the code", "toolUseId": "tc-agent"})
    )

    (agent_state_dir / "claude_session_id_history").write_text(f"{_MAIN_SESSION}\n")

    # Shared .tickets dir: all six steps stamped with the SAME agent name.
    tickets_dir = tmp_path / ".tickets"
    tickets_dir.mkdir()
    (tickets_dir / "cod-main1.md").write_text(_step_ticket("cod-main1", "in_progress", "Main work one"))
    (tickets_dir / "cod-main2.md").write_text(_step_ticket("cod-main2", "closed", "Main work two", summary="wrapped up"))
    (tickets_dir / "cod-sub1.md").write_text(_step_ticket("cod-sub1", "closed", "Sub explore", summary="explored"))
    (tickets_dir / "cod-sub2.md").write_text(_step_ticket("cod-sub2", "in_progress", "Sub analyze"))
    (tickets_dir / "cod-sub3.md").write_text(_step_ticket("cod-sub3", "open", "Sub pending step"))

    return agent_state_dir, claude_config_dir, tickets_dir


def test_main_view_excludes_subagent_steps_and_subagent_view_owns_them(tmp_path: Path) -> None:
    agent_state_dir, claude_config_dir, tickets_dir = _setup(tmp_path)

    session_watcher = AgentSessionWatcher(
        agent_id="wolf-id",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda _aid, _evts: None,
    )
    tickets_watcher = AgentTicketsWatcher(
        agent_id="wolf-id",
        agent_name=_AGENT_NAME,
        tickets_dir=tickets_dir,
        on_events=lambda _aid, _evts: None,
        attribution_provider=session_watcher.get_step_attribution,
    )

    # Main progress view: only the main agent's own steps, never the subagent's
    # (started/closed -> excluded by transition; pending -> excluded by title).
    main = tickets_watcher.get_enrichment()
    assert sorted(main.keys()) == ["cod-main1", "cod-main2"]
    assert main["cod-main2"]["summary"] == "wrapped up"

    # The subagent's own conversation surfaces exactly its three steps, with
    # enrichment intact -- the closed one keeps its summary, the pending one
    # (cod-sub3, attributed by `tk create --step "Sub pending step"`) is present.
    sub = tickets_watcher.get_enrichment(session_id=_SUB_SESSION)
    assert sorted(sub.keys()) == ["cod-sub1", "cod-sub2", "cod-sub3"]
    assert sub["cod-sub1"]["summary"] == "explored"
    assert sub["cod-sub3"]["status"] == "open"


def test_attribution_reads_signals_from_both_transcripts(tmp_path: Path) -> None:
    """The session watcher's attribution must surface transition ids and create
    titles from BOTH the main and the (different-session) subagent transcript --
    the subagent transcript is excluded from the main event stream, so without
    this dedicated scan its steps would be unattributable."""
    agent_state_dir, claude_config_dir, _tickets_dir = _setup(tmp_path)
    session_watcher = AgentSessionWatcher(
        agent_id="wolf-id",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        on_events=lambda _aid, _evts: None,
    )
    attribution = session_watcher.get_step_attribution()

    assert set(attribution.transition_ids_by_session[_MAIN_SESSION]) == {"cod-main1", "cod-main2"}
    assert set(attribution.transition_ids_by_session[_SUB_SESSION]) == {"cod-sub1", "cod-sub2"}
    assert "Sub pending step" in attribution.create_titles_by_session[_SUB_SESSION]
    assert _MAIN_SESSION in attribution.main_session_ids
    assert _SUB_SESSION not in attribution.main_session_ids
