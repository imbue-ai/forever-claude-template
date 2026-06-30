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
    # A still-launching (init) browser is labeled as starting, ahead of any owner read.
    assert "starting" in fleet._owner_label({"lifecycle": "init", "controller": "human"}, me)
    # The explicit crashed lifecycle reads as crashed even without the derived `crashed` flag.
    assert "crashed" in fleet._owner_label({"lifecycle": "crashed", "controller": "human"}, me)


@pytest.mark.parametrize(
    "event,expected",
    [
        ({"type": "done", "result": "ok"}, fleet._EXIT_OK),
        ({"type": "error", "text": "boom"}, fleet._EXIT_ERROR),
        ({"type": "preempted"}, fleet._EXIT_PREEMPTED),
        ({"type": "busy_human"}, fleet._EXIT_BUSY),
        ({"type": "busy_agent"}, fleet._EXIT_BUSY),
        ({"type": "timed_out"}, fleet._EXIT_TIMEOUT),
        # a task/hold whose acquire found the browser still launching -> wait and retry.
        ({"type": "starting"}, fleet._EXIT_BUSY),
        ({"type": "crashed"}, fleet._EXIT_ERROR),
        ({"type": "thinking", "text": "..."}, None),
        ({"type": "action", "text": "click"}, None),
        ({"type": "waiting", "busy_name": "Bob"}, None),
        ({"type": "acquired"}, None),
        ({"type": "held"}, None),
    ],
)
def test_render_event_exit_codes(event: dict, expected: int | None) -> None:
    # The exit code an agent branches on is the load-bearing CLI contract.
    assert fleet._render_event(event, browser_name="alex-smith") == expected


def test_parser_accepts_task_flags() -> None:
    parser = fleet._build_parser()
    args = parser.parse_args(["task", "alex-smith", "do it", "--reclaim", "--no-wait", "--max-wait", "30"])
    assert args.name == "alex-smith" and args.prompt == "do it"
    assert args.reclaim is True and args.no_wait is True and args.max_wait == 30.0
    assert fleet._build_parser().parse_args(["ls"]).func is fleet.cmd_ls
    new = fleet._build_parser().parse_args(["new"])
    assert new.func is fleet.cmd_new and new.name is None  # name optional, defaults to None
    assert fleet._build_parser().parse_args(["new", "my-browser"]).name == "my-browser"
    assert fleet._build_parser().parse_args(["unlock", "riley-jones"]).func is fleet.cmd_release


@pytest.mark.parametrize(
    "payload,kind,expected",
    [
        ({"ok": True, "url": "https://x", "title": "X", "elements": "[1]<a>", "tabs": []}, "state", fleet._EXIT_OK),
        ({"ok": True, "screenshot_path": "/tmp/s.png"}, "screenshot", fleet._EXIT_OK),
        ({"ok": True, "clicked": 5}, "click", fleet._EXIT_OK),
        # busy_human when the agent was ENROLLED to resume (a state-changing command) means
        # "the human is driving -- you're queued to resume; stop and wait for the wake", i.e.
        # preempted (exit 2). A read-only `state` peek enrols nothing, so the same status is a
        # generic busy (exit 3, move on) -- it must NOT claim the agent is queued.
        ({"ok": False, "status": "busy_human", "enqueued": True}, "click", fleet._EXIT_PREEMPTED),
        ({"ok": False, "status": "busy_human", "enqueued": False}, "state", fleet._EXIT_BUSY),
        ({"ok": False, "status": "busy_agent", "enqueued": True}, "click", fleet._EXIT_BUSY),
        ({"ok": False, "status": "busy_agent", "enqueued": False}, "state", fleet._EXIT_BUSY),
        # the fleet is still restoring saved browsers -> try again (busy), not a hard error.
        ({"ok": False, "status": "initializing"}, "click", fleet._EXIT_BUSY),
        # the browser itself is still launching (init) -> try again (busy), non-fatal.
        ({"ok": False, "status": "starting"}, "click", fleet._EXIT_BUSY),
        ({"ok": False, "status": "crashed"}, "state", fleet._EXIT_ERROR),
        ({"ok": False, "status": "lost_control", "enqueued": True}, "click", fleet._EXIT_PREEMPTED),
        ({"ok": False, "status": "lost_control", "enqueued": False}, "state", fleet._EXIT_BUSY),
        ({"ok": False, "status": "stale_index", "error": "run state"}, "click", fleet._EXIT_ERROR),
        ({"ok": False, "status": "timed_out"}, "state", fleet._EXIT_TIMEOUT),
        ({"ok": False, "status": "error", "error": "boom"}, "click", fleet._EXIT_ERROR),
    ],
)
def test_render_action_exit_codes(payload: dict, kind: str, expected: int) -> None:
    # The exit code an agent branches on per direct command is load-bearing.
    assert fleet._render_action(payload, browser_name="alex-smith", kind=kind) == expected


def test_parser_accepts_direct_verbs() -> None:
    p = fleet._build_parser()
    # The browser arg is a NAME (string), not an int: it must NOT be int-coerced.
    assert p.parse_args(["state", "alex-smith"]).name == "alex-smith"
    assert p.parse_args(["open", "alex-smith", "https://x"]).func is fleet.cmd_open
    click = p.parse_args(["click", "alex-smith", "18"])
    assert click.func is fleet.cmd_click and click.name == "alex-smith" and click.index == 18
    typed = p.parse_args(["input", "alex-smith", "3", "hello there"])
    assert typed.func is fleet.cmd_input and typed.text == "hello there"
    assert p.parse_args(["screenshot", "riley-jones"]).func is fleet.cmd_screenshot
    tab = p.parse_args(["tab", "alex-smith", "switch", "1"])
    assert tab.func is fleet.cmd_tab and tab.action == "switch" and tab.index == 1
    assert p.parse_args(["acquire", "alex-smith", "--reclaim"]).reclaim is True
    assert p.parse_args(["ls", "--include-tabs"]).include_tabs is True
    close = p.parse_args(["close", "morgan-lee"])
    assert close.func is fleet.cmd_close and close.name == "morgan-lee"


def test_pull_in_pane_opens_each_browser_in_its_own_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    # A user-started agent surfaces each browser as its OWN pane (--new-group), beside
    # its own chat (--relative-to self), not tabbed into an existing browser pane. The
    # session ref keys on the NAME.
    calls: list[tuple] = []
    monkeypatch.setattr(fleet, "_layout", lambda *a, **k: calls.append(a) or True)
    monkeypatch.delenv("BROWSER_FLEET_ANCHOR", raising=False)
    fleet._pull_in_pane("alex-smith")
    assert calls and "--new-group" in calls[0] and "right" in calls[0] and "self" in calls[0]
    assert any("session=alex-smith" in arg for arg in calls[0])


def test_pull_in_pane_warns_cleanly_when_it_cant_show_a_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    # A background/sub-agent (no chat in this UI) can't land the split: we attempt it,
    # then warn in one clean line -- never crash, never leak the raw 5s "not registered"
    # error (the browser is still running).
    monkeypatch.setattr(fleet, "_layout", lambda *a, **k: False)  # layout never lands
    monkeypatch.delenv("BROWSER_FLEET_ANCHOR", raising=False)
    fleet._pull_in_pane("riley-jones")  # must not raise


def test_cmd_new_pulls_a_pane_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # "Open a new browser" should visibly open its pane (by the returned name), not wait
    # for the first command. The daemon returns the chosen name as `name`.
    pulled: list[str] = []
    monkeypatch.setattr(fleet, "_request", lambda *a, **k: (200, {"name": "alex-smith"}))
    monkeypatch.setattr(fleet, "_pull_in_pane", lambda name: pulled.append(name))
    args = fleet._build_parser().parse_args(["new"])
    assert fleet.cmd_new(args) == fleet._EXIT_OK
    assert pulled == ["alex-smith"]


def test_cmd_new_sends_chosen_name_and_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    # A typed name is forwarded in the body; an invalid name (400) is a usage error, a
    # duplicate / fleet-full (409) is "try later" (busy).
    sent: list[dict] = []

    def fake_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        sent.append(body or {})
        return 200, {"name": "my-browser"}

    monkeypatch.setattr(fleet, "_request", fake_request)
    monkeypatch.setattr(fleet, "_pull_in_pane", lambda name: None)
    args = fleet._build_parser().parse_args(["new", "my-browser"])
    assert fleet.cmd_new(args) == fleet._EXIT_OK
    assert sent == [{"name": "my-browser"}]

    monkeypatch.setattr(fleet, "_request", lambda *a, **k: (400, {"error": "bad name"}))
    assert fleet.cmd_new(fleet._build_parser().parse_args(["new", "Bad Name"])) == fleet._EXIT_USAGE
    monkeypatch.setattr(fleet, "_request", lambda *a, **k: (409, {"error": "already in use"}))
    assert fleet.cmd_new(fleet._build_parser().parse_args(["new", "dup"])) == fleet._EXIT_BUSY


def test_parser_accepts_handoff_and_request_human_alias() -> None:
    p = fleet._build_parser()
    a = p.parse_args(["handoff", "riley-jones", "solve the captcha"])
    assert a.func is fleet.cmd_handoff and a.name == "riley-jones" and a.reason == "solve the captcha"
    # The alias resolves to the same command, and reason defaults when omitted.
    b = fleet._build_parser().parse_args(["request-human", "alex-smith"])
    assert b.func is fleet.cmd_handoff and b.name == "alex-smith" and b.reason == "human verification needed"


def test_cmd_handoff_pulls_pane_and_returns_preempted(monkeypatch: pytest.MonkeyPatch) -> None:
    # A successful handoff surfaces the pane (so the human sees what to solve) and exits
    # PREEMPTED so the agent stops and waits to be woken to resume.
    pulled: list[str] = []
    monkeypatch.setattr(fleet, "_request", lambda *a, **k: (200, {"ok": True, "status": "handed_off"}))
    monkeypatch.setattr(fleet, "_pull_in_pane", lambda name: pulled.append(name))
    args = fleet._build_parser().parse_args(["handoff", "alex-smith", "solve the captcha"])
    assert fleet.cmd_handoff(args) == fleet._EXIT_PREEMPTED
    assert pulled == ["alex-smith"]


def test_cmd_handoff_not_owner_still_tells_agent_to_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    # Handing off a browser you no longer hold (a human already grabbed it) still exits
    # PREEMPTED -- the agent isn't in control, so it should stop, not treat it as an error.
    monkeypatch.setattr(fleet, "_request", lambda *a, **k: (200, {"ok": False, "status": "not_owner"}))
    monkeypatch.setattr(fleet, "_pull_in_pane", lambda name: None)
    args = fleet._build_parser().parse_args(["handoff", "alex-smith"])
    assert fleet.cmd_handoff(args) == fleet._EXIT_PREEMPTED
