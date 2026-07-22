"""End-to-end tests for the minds OpenHost app.

The harness builds the Dockerfile, deploys the app through the real OpenHost
router install path, and fronts it with real subdomain routing and owner
auth — ``stack.owner_session`` behaves like the logged-in owner's browser.
The real openhost-latchkey app is deployed alongside as the latchkey service
provider (see conftest.py).
"""

import json
import subprocess
import time

CHAT_AGENT_TIMEOUT = 180.0


def _podman_exec(container_name: str, *cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["podman", "exec", container_name, *cmd],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _wait_for_agents(stack, min_count: int, timeout: float) -> list[dict]:
    deadline = time.monotonic() + timeout
    agents: list[dict] = []
    while time.monotonic() < deadline:
        resp = stack.owner_session.get(f"{stack.url}/api/agents", timeout=10)
        if resp.status_code == 200:
            agents = resp.json().get("agents", [])
            if len(agents) >= min_count:
                return agents
        time.sleep(3)
    raise AssertionError(f"Expected >= {min_count} agents, got: {agents}")


def _wait_for_real_ui(stack, timeout: float) -> None:
    """Deploy readiness is satisfied by the boot placeholder page; poll until
    system_interface takes over the port and serves the real UI."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            resp = stack.owner_session.get(f"{stack.url}/", timeout=10)
            last = f"{resp.status_code}: {resp.text[:200]}"
            if resp.status_code == 200 and "System Interface" in resp.text:
                return
        except Exception as e:
            last = str(e)
        time.sleep(3)
    raise AssertionError(f"Real UI never appeared; last response: {last}")


def test_ui_served_through_router(stack):
    _wait_for_real_ui(stack, timeout=240)


def test_ui_requires_auth(stack):
    """Without the owner session, the router must not serve the workspace."""
    import requests

    resp = requests.get(f"{stack.url}/", timeout=30, allow_redirects=False)
    assert resp.status_code in (302, 401, 403), resp.status_code


def test_first_boot_creates_chat_agent(stack, app_data_dir, app_name):
    """Bootstrap creates the initial chat agent, named after the app. (The
    services agent also appears in the raw list; the frontend filters it out
    by its is_primary label.)"""
    agents = _wait_for_agents(stack, min_count=2, timeout=CHAT_AGENT_TIMEOUT)
    names = {a["name"] for a in agents}
    assert app_name in names, agents
    marker = app_data_dir / "mngr" / "code" / "runtime" / "initial_chat_created"
    assert marker.exists()


def test_host_env_wiring(stack, container_name):
    """The entrypoint wires the LLM gateway, latchkey, and sandbox env vars
    into the host env file every agent shell sources."""
    result = _podman_exec(container_name, "cat", "/mngr/env")
    assert result.returncode == 0, result.stderr
    env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    router_url = env["OPENHOST_ROUTER_URL"]
    assert env["ANTHROPIC_BASE_URL"] == f"{router_url}/api/services/v2/call/llm/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == env["OPENHOST_APP_TOKEN"]
    assert env["LATCHKEY_GATEWAY"] == f"{router_url}/api/services/v2/call/latchkey"
    assert env["IS_SANDBOX"] == "1"
    assert env["MNGR_HOST_DIR"] == "/mngr"


def test_claude_agent_running_in_tmux(stack, container_name, app_name):
    """The chat agent's Claude Code TUI actually launched (bypass-permissions
    mode works because IS_SANDBOX=1)."""
    session = f"mngr-{app_name}"
    deadline = time.monotonic() + CHAT_AGENT_TIMEOUT
    pane = ""
    while time.monotonic() < deadline:
        result = _podman_exec(container_name, "tmux", "capture-pane", "-t", f"{session}:0", "-p")
        pane = result.stdout
        if "bypass permissions" in pane:
            return
        time.sleep(3)
    raise AssertionError(f"Claude TUI not detected in {session}:0; pane:\n{pane}")


def test_terminal_service_registered(stack, app_data_dir):
    """Services register in applications.toml and are proxied under
    /service/<name>/ by system_interface."""
    apps_toml = app_data_dir / "mngr" / "code" / "runtime" / "applications.toml"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if apps_toml.exists() and "terminal" in apps_toml.read_text():
            break
        time.sleep(2)
    else:
        raise AssertionError("terminal service never registered in applications.toml")

    resp = stack.owner_session.get(f"{stack.url}/service/terminal/", timeout=30)
    assert resp.status_code == 200


def _agent_env_curl(container_name: str, url_expr: str, *extra: str) -> tuple[str, str]:
    """Run curl inside the container with the agent env (/mngr/env) loaded,
    returning (body, status_code) — exercising exactly what the latchkey
    skill tells agents to run."""
    cmd = (
        'set -a; . /mngr/env; set +a; '
        f'curl -sS -w "\\n%{{http_code}}" -H "Authorization: Bearer $OPENHOST_APP_TOKEN" {" ".join(extra)} "{url_expr}"'
    )
    result = _podman_exec(container_name, "bash", "-c", cmd)
    assert result.returncode == 0, result.stderr
    body, status = result.stdout.rsplit("\n", 1)
    return body, status.strip()


def test_latchkey_services_reachable_from_agents(stack, container_name):
    """The manifest's latchkey-meta/services-read grant plus the router
    service proxy let agents list latchkey services from inside the mind."""
    body, status = _agent_env_curl(container_name, "$LATCHKEY_GATEWAY/services")
    assert status == "200", (status, body)
    assert "services" in json.loads(body)


def test_latchkey_ungranted_proxy_call_returns_grant_url(stack, container_name):
    """An ungranted third-party call comes back 403 permission_required with
    a grant_url — the plain-flow contract the latchkey skill relies on."""
    body, status = _agent_env_curl(
        container_name, "$LATCHKEY_GATEWAY/proxy/https://slack.com/api/conversations.list"
    )
    assert status == "403", (status, body)
    payload = json.loads(body)
    assert payload["error"] == "permission_required", payload
    assert payload.get("grant_url"), payload


def test_state_survives_restart(stack, container_name, app_data_dir):
    """Warm boot: container restart preserves the mind and does not re-run
    first-boot creation; the UI comes back."""
    agents_dir = app_data_dir / "mngr" / "agents"
    agent_ids_before = sorted(p.name for p in agents_dir.iterdir())
    assert len(agent_ids_before) == 2, agent_ids_before

    subprocess.run(["podman", "restart", container_name], check=True, capture_output=True, timeout=120)

    _wait_for_real_ui(stack, timeout=240)

    agent_ids_after = sorted(p.name for p in agents_dir.iterdir())
    assert agent_ids_after == agent_ids_before
    marker = app_data_dir / "mngr" / "code" / "runtime" / "initial_chat_created"
    assert marker.exists()
