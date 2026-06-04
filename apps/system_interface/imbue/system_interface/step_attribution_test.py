"""Unit tests for tk step -> session attribution.

These exercise the pure pieces: pulling `tk create --step` titles and
`Updated <id> ->` transition ids out of raw transcript lines, and joining those
signals onto a set of step records to decide which session owns each step.
"""

from __future__ import annotations

import json

from imbue.system_interface.step_attribution import StepAttribution
from imbue.system_interface.step_attribution import attribute_steps
from imbue.system_interface.step_attribution import extract_create_titles
from imbue.system_interface.step_attribution import extract_step_signals


def _bash_create_line(command: str, uuid: str = "u1") -> str:
    """A raw assistant JSONL line whose single tool_use is a Bash `tk create`."""
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tc1", "name": "Bash", "input": {"command": command}}],
            },
        }
    )


def _transition_line(text: str, uuid: str = "u2") -> str:
    """A raw user JSONL line carrying a tool_result whose output is `text`."""
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tc1", "content": text}]},
        }
    )


# --- extract_create_titles ---


def test_extract_create_titles_handles_batched_creates_with_parens() -> None:
    """A single Bash command can batch several `tk create --step` calls; every
    title is extracted, including one containing parentheses (which a
    naive split on `)` would corrupt). Mirrors the real repro command shape."""
    command = (
        'S1=$(tk create --step "Explore the directory"); '
        'S2=$(tk create --step "Analyze build process (vite config)"); '
        'S3=$(tk create --step "Examine backend")'
    )
    assert extract_create_titles(command) == [
        "Explore the directory",
        "Analyze build process (vite config)",
        "Examine backend",
    ]


def test_extract_create_titles_ignores_non_step_creates_and_mentions() -> None:
    """Only `tk/ticket create --step` invocations yield a title. A regular
    create, an unrelated command that merely mentions a tk verb, and a
    `--only-steps` listing all yield nothing."""
    assert extract_create_titles('tk create "Regular ticket"') == []
    assert extract_create_titles('git commit -m "tk close foo"') == []
    assert extract_create_titles("tk ls --only-steps") == []


def test_extract_create_titles_handles_single_quotes_and_super() -> None:
    assert extract_create_titles("S1=$(tk super create --step 'Quoted with single')") == ["Quoted with single"]


# --- extract_step_signals ---


def test_extract_step_signals_pulls_creates_and_transitions() -> None:
    lines = [
        _bash_create_line('S1=$(tk create --step "Do A") && S2=$(tk create --step "Do B")', uuid="c1"),
        _transition_line("Updated cod-a1 -> in_progress", uuid="t1"),
        _transition_line("Updated cod-a1 -> closed", uuid="t2"),
    ]
    signals = extract_step_signals(lines)
    assert signals.create_titles == ("Do A", "Do B")
    # The same id transitioning twice (start then close) both count -- the id
    # set just records that this session ran the step.
    assert signals.transition_ids == ("cod-a1", "cod-a1")


def test_extract_step_signals_tolerates_malformed_lines() -> None:
    lines = ["not json", "", _bash_create_line('S1=$(tk create --step "Survivor")')]
    assert extract_step_signals(lines).create_titles == ("Survivor",)


# --- attribute_steps ---


def _attr(
    transitions: dict[str, tuple[str, ...]],
    creates: dict[str, tuple[str, ...]],
    main: tuple[str, ...],
) -> StepAttribution:
    return StepAttribution(
        transition_ids_by_session=transitions,
        create_titles_by_session=creates,
        main_session_ids=main,
    )


def test_started_steps_attributed_definitively_by_transition() -> None:
    """A started/closed step belongs to whichever session printed its
    transition -- regardless of which session also created the same title."""
    step_records = [("cod-main", "Shared title", "closed"), ("cod-sub", "Shared title", "closed")]
    attribution = _attr(
        transitions={"main-sess": ("cod-main",), "agent-sub": ("cod-sub",)},
        creates={"main-sess": ("Shared title",), "agent-sub": ("Shared title",)},
        main=("main-sess",),
    )
    owner = attribute_steps(step_records, attribution)
    assert owner == {"cod-main": "main-sess", "cod-sub": "agent-sub"}


def test_pending_step_attributed_by_title_to_creating_session() -> None:
    """A pending step (open, no transition) is matched by title to the session
    whose transcript created it."""
    step_records = [("cod-pending", "Only the subagent made this", "open")]
    attribution = _attr(
        transitions={},
        creates={"agent-sub": ("Only the subagent made this",)},
        main=("main-sess",),
    )
    owner = attribute_steps(step_records, attribution)
    assert owner == {"cod-pending": "agent-sub"}


def test_residual_keeps_pending_off_a_session_whose_create_already_started() -> None:
    """When the main session created+started a step titled T, and the subagent
    created a *pending* step also titled T, residual counting routes the pending
    one to the subagent: the main session's create of T is already spent on its
    started step."""
    step_records = [
        ("cod-main-started", "Build it", "closed"),
        ("cod-sub-pending", "Build it", "open"),
    ]
    attribution = _attr(
        transitions={"main-sess": ("cod-main-started",)},
        creates={"main-sess": ("Build it",), "agent-sub": ("Build it",)},
        main=("main-sess",),
    )
    owner = attribute_steps(step_records, attribution)
    assert owner == {"cod-main-started": "main-sess", "cod-sub-pending": "agent-sub"}


def test_unattributable_pending_step_is_none() -> None:
    """A pending step whose title was never created in any transcript cannot be
    attributed; it resolves to None (the caller defaults it to the main view)."""
    owner = attribute_steps([("cod-orphan", "No create anywhere", "open")], _attr({}, {}, ("main-sess",)))
    assert owner == {"cod-orphan": None}


def test_same_title_pending_in_two_sessions_degrades_gracefully() -> None:
    """The acknowledged ambiguous case: the same title is pending in two
    sessions. Attribution must not crash and must stay count-correct -- both
    steps get attributed, distributed across the two creating sessions (which
    one lands where is arbitrary, the accepted cosmetic imperfection)."""
    step_records = [("cod-x", "Same title", "open"), ("cod-y", "Same title", "open")]
    attribution = _attr(
        transitions={},
        creates={"agent-a": ("Same title",), "agent-b": ("Same title",)},
        main=("main-sess",),
    )
    owner = attribute_steps(step_records, attribution)
    assert set(owner.keys()) == {"cod-x", "cod-y"}
    # Both attributed, one to each session (no double-assignment, nothing dropped).
    assert sorted(v for v in owner.values() if v is not None) == ["agent-a", "agent-b"]


def test_empty_inputs_do_not_crash() -> None:
    assert attribute_steps([], _attr({}, {}, ())) == {}
