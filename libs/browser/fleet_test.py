import pytest

from browser import fleet


def test_daemon_url_prefers_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_BROWSER_SERVICE_URL", "http://example:9000/")
    assert fleet._daemon_url() == "http://example:9000"


def test_daemon_url_reads_applications_registry(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("MINDS_BROWSER_SERVICE_URL", raising=False)
    registry = tmp_path / "applications.toml"
    registry.write_text(
        '[[applications]]\nname = "web"\nurl = "http://localhost:8080"\n'
        '[[applications]]\nname = "browser"\nurl = "http://localhost:8081"\n'
    )
    monkeypatch.setenv("MINDS_APPLICATIONS_FILE", str(registry))
    assert fleet._daemon_url() == "http://localhost:8081"


def test_daemon_url_falls_back_to_localhost(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("MINDS_BROWSER_SERVICE_URL", raising=False)
    monkeypatch.setenv("MINDS_APPLICATIONS_FILE", str(tmp_path / "missing.toml"))
    assert fleet._daemon_url() == "http://127.0.0.1:8081"


def test_agent_headers_requires_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        fleet._agent_headers()
    assert exc.value.code == fleet._EXIT_USAGE


def test_owner_label_distinguishes_self_other_free_and_pinned() -> None:
    me = "alice"
    assert fleet._owner_label({"controller": "agent", "owner_agent_id": "alice", "owner_name": "Alice"}, me) == "you"
    other = {"controller": "agent", "owner_agent_id": "bob", "owner_name": "Bob"}
    assert fleet._owner_label(other, me) == "agent Bob"
    assert fleet._owner_label({"controller": "human", "human_pinned": False}, me) == "free"
    assert fleet._owner_label({"controller": "human", "human_pinned": True}, me) == "human (took control)"


@pytest.mark.parametrize(
    "event,expected",
    [
        ({"type": "done", "result": "ok"}, fleet._EXIT_OK),
        ({"type": "error", "text": "boom"}, fleet._EXIT_ERROR),
        ({"type": "preempted"}, fleet._EXIT_PREEMPTED),
        ({"type": "busy_human"}, fleet._EXIT_BUSY),
        ({"type": "busy_agent"}, fleet._EXIT_BUSY),
        ({"type": "timed_out"}, fleet._EXIT_TIMEOUT),
        ({"type": "thinking", "text": "..."}, None),
        ({"type": "action", "text": "click"}, None),
        ({"type": "waiting", "busy_name": "Bob"}, None),
        ({"type": "acquired"}, None),
        ({"type": "held"}, None),
    ],
)
def test_render_event_exit_codes(event: dict, expected: int | None) -> None:
    # The exit code an agent branches on is the load-bearing CLI contract.
    assert fleet._render_event(event, browser_id=0) == expected


def test_parser_accepts_task_flags() -> None:
    parser = fleet._build_parser()
    args = parser.parse_args(["task", "2", "do it", "--reclaim", "--no-wait", "--max-wait", "30"])
    assert args.id == 2 and args.prompt == "do it"
    assert args.reclaim is True and args.no_wait is True and args.max_wait == 30.0
    assert fleet._build_parser().parse_args(["ls"]).func is fleet.cmd_ls
    assert fleet._build_parser().parse_args(["new"]).func is fleet.cmd_new
    assert fleet._build_parser().parse_args(["unlock", "1"]).func is fleet.cmd_release


@pytest.mark.parametrize(
    "payload,kind,expected",
    [
        ({"ok": True, "url": "https://x", "title": "X", "elements": "[1]<a>", "tabs": []}, "state", fleet._EXIT_OK),
        ({"ok": True, "screenshot_path": "/tmp/s.png"}, "screenshot", fleet._EXIT_OK),
        ({"ok": True, "clicked": 5}, "click", fleet._EXIT_OK),
        # busy_human now means "the human is driving -- you're queued to resume; stop
        # and wait for the wake", i.e. preempted (exit 2), not a generic busy (exit 3).
        ({"ok": False, "status": "busy_human"}, "click", fleet._EXIT_PREEMPTED),
        ({"ok": False, "status": "busy_agent"}, "state", fleet._EXIT_BUSY),
        ({"ok": False, "status": "lost_control"}, "click", fleet._EXIT_PREEMPTED),
        ({"ok": False, "status": "stale_index", "error": "run state"}, "click", fleet._EXIT_ERROR),
        ({"ok": False, "status": "timed_out"}, "state", fleet._EXIT_TIMEOUT),
        ({"ok": False, "status": "error", "error": "boom"}, "click", fleet._EXIT_ERROR),
    ],
)
def test_render_action_exit_codes(payload: dict, kind: str, expected: int) -> None:
    # The exit code an agent branches on per direct command is load-bearing.
    assert fleet._render_action(payload, browser_id=0, kind=kind) == expected


def test_parser_accepts_direct_verbs() -> None:
    p = fleet._build_parser()
    assert p.parse_args(["state", "0"]).func is fleet.cmd_state
    assert p.parse_args(["open", "0", "https://x"]).func is fleet.cmd_open
    click = p.parse_args(["click", "1", "18"])
    assert click.func is fleet.cmd_click and click.id == 1 and click.index == 18
    typed = p.parse_args(["input", "0", "3", "hello there"])
    assert typed.func is fleet.cmd_input and typed.text == "hello there"
    assert p.parse_args(["screenshot", "2"]).func is fleet.cmd_screenshot
    tab = p.parse_args(["tab", "0", "switch", "1"])
    assert tab.func is fleet.cmd_tab and tab.action == "switch" and tab.index == 1
    assert p.parse_args(["acquire", "0", "--reclaim"]).reclaim is True
    assert p.parse_args(["ls", "--include-tabs"]).include_tabs is True
    close = p.parse_args(["close", "4"])
    assert close.func is fleet.cmd_close and close.id == 4
