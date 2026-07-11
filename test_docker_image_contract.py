"""Smoke-test the FCT Docker image contract.

This is intentionally narrower than the Minds Electron e2e test: it builds the
template Dockerfile directly, then checks the image-level and first-boot volume
contracts that a Docker-backed workspace depends on.

The test is opt-in because building the full FCT image is slow and network
heavy. Run with:

    FCT_DOCKER_IMAGE_CONTRACT=1 uv run pytest -s test_docker_image_contract.py

The provider boot contract is slower and starts a real Docker host via the FCT
template stack before checking SSH and SFTP:

    FCT_DOCKER_PROVIDER_CONTRACT=1 uv run pytest -s test_docker_image_contract.py::test_fct_docker_provider_boot_contract
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import paramiko
import pytest

_REPO_ROOT = Path(__file__).parent
_DOCKERFILE = os.environ.get("FCT_DOCKERFILE", "Dockerfile")
_IMAGE_TAG = os.environ.get("FCT_DOCKER_IMAGE_TAG", "fct-docker-image-contract:local")
_BUILD_TIMEOUT_SECONDS = int(os.environ.get("FCT_DOCKER_BUILD_TIMEOUT_SECONDS", "3600"))
_RUN_TIMEOUT_SECONDS = int(os.environ.get("FCT_DOCKER_RUN_TIMEOUT_SECONDS", "300"))
_PROVIDER_TEMPLATE = os.environ.get("FCT_DOCKER_PROVIDER_TEMPLATE", "docker-nixos")
_IS_NIXOS_DOCKERFILE = Path(_DOCKERFILE).as_posix().lstrip("./") == "nix/Dockerfile"
_EXPECTED_NODE_MAJOR = os.environ.get(
    "FCT_EXPECTED_NODE_MAJOR",
    "24" if _IS_NIXOS_DOCKERFILE else "20",
)


pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.docker,
    pytest.mark.timeout(_BUILD_TIMEOUT_SECONDS + 2 * _RUN_TIMEOUT_SECONDS + 120),
]


def _docker_contract_enabled() -> bool:
    return os.environ.get("FCT_DOCKER_IMAGE_CONTRACT", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _docker_provider_contract_enabled() -> bool:
    return os.environ.get("FCT_DOCKER_PROVIDER_CONTRACT", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _run(
    args: list[str],
    *,
    cwd: Path = _REPO_ROOT,
    env: dict[str, str] | None = None,
    timeout: int = _RUN_TIMEOUT_SECONDS,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        command = " ".join(shlex.quote(arg) for arg in args)
        raise AssertionError(
            f"command failed with exit {result.returncode}: {command}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _docker_available() -> bool:
    try:
        result = _run(["docker", "version"], timeout=30, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _build_image() -> None:
    _run(
        ["docker", "build", "--file", _DOCKERFILE, "--tag", _IMAGE_TAG, "."],
        timeout=_BUILD_TIMEOUT_SECONDS,
    )


def _run_in_image(script: str, *, volume_name: str | None = None) -> None:
    args = ["docker", "run", "--rm", "--workdir", "/"]
    args.extend(["--env", f"FCT_EXPECTED_NODE_MAJOR={_EXPECTED_NODE_MAJOR}"])
    if volume_name is not None:
        args.extend(["--volume", f"{volume_name}:/mngr"])
    args.extend([_IMAGE_TAG, "bash", "-lc", script])
    _run(args)


def _create_volume(name: str) -> None:
    _run(["docker", "volume", "create", name], timeout=30)


def _remove_volume(name: str) -> None:
    _run(["docker", "volume", "rm", "-f", name], timeout=30, check=False)


def _remove_docker_resources_with_prefix(prefix: str) -> None:
    containers = _run(
        ["docker", "ps", "-aq", "--filter", f"name={prefix}"],
        timeout=30,
        check=False,
    ).stdout.split()
    if containers:
        _run(["docker", "rm", "-f", *containers], timeout=60, check=False)

    volumes = _run(
        ["docker", "volume", "ls", "--format", "{{.Name}}"],
        timeout=30,
        check=False,
    ).stdout.splitlines()
    for volume in volumes:
        if volume.startswith(prefix):
            _run(["docker", "volume", "rm", "-f", volume], timeout=30, check=False)


def _write_pytest_project_config(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_text = (_REPO_ROOT / ".mngr" / "settings.toml").read_text()
    (config_dir / "settings.toml").write_text("is_allowed_in_pytest = true\n" + settings_text)


def _mngr_env(tmp_path: Path, prefix: str, project_config_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "MNGR_HOST_DIR": str(tmp_path / "mngr"),
            "MNGR_PREFIX": prefix,
            "MNGR_PROJECT_CONFIG_DIR": str(project_config_dir),
            "MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME": "runc",
            "MNGR__PROVIDERS__DOCKER__ISOLATE_HOST_VOLUMES": "false",
            "MNGR__PROVIDERS__AZURE__IS_ENABLED": "false",
            "MNGR__PROVIDERS__GCP__IS_ENABLED": "false",
            "MNGR__PROVIDERS__MODAL__IS_ENABLED": "false",
            "MNGR__PROVIDERS__OVH__IS_ENABLED": "false",
            "MNGR__PROVIDERS__VULTR__IS_ENABLED": "false",
        }
    )
    return env


def _created_event(stdout: str) -> dict[str, str]:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "created":
            return event
    raise AssertionError(f"mngr create did not emit a created event:\n{stdout}")


def _connect_ssh(created: dict[str, str]) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=created["ssh_host"],
        port=int(created["ssh_port"]),
        username=created["ssh_user"],
        key_filename=created["ssh_key_path"],
        look_for_keys=False,
        allow_agent=False,
        timeout=30,
    )
    return client


def _exec_ssh(client: paramiko.SSHClient, command: str) -> str:
    _stdin, stdout, stderr = client.exec_command(command, timeout=420)
    exit_status = stdout.channel.recv_exit_status()
    stdout_text = stdout.read().decode()
    stderr_text = stderr.read().decode()
    if exit_status != 0:
        raise AssertionError(
            f"SSH command failed with exit {exit_status}: {command}\n"
            f"stdout:\n{stdout_text}\n"
            f"stderr:\n{stderr_text}"
        )
    return stdout_text


def test_fct_dockerfile_image_contract() -> None:
    if not _docker_contract_enabled():
        pytest.skip("set FCT_DOCKER_IMAGE_CONTRACT=1 to build and smoke-test the FCT Docker image")
    if not _docker_available():
        pytest.skip("docker CLI/daemon is not available")

    _build_image()

    _run_in_image(
        r"""
set -euo pipefail

echo "checking provider bootstrap commands on default PATH"
provider_bootstrap_commands=(
  git
  curl
  tmux
  rsync
  jq
  xxd
  flock
)

for command_name in "${provider_bootstrap_commands[@]}"; do
  command -v "$command_name" >/dev/null
done

test -f /etc/ssl/certs/ca-certificates.crt
test -x /usr/sbin/sshd

export PATH="/root/.local/bin:$PATH"
if [ -f /etc/profile.d/fct_path.sh ]; then
  . /etc/profile.d/fct_path.sh
fi

echo "checking image-level commands"
required_commands=(
  bash
  git
  curl
  sshd
  tmux
  flock
  rsync
  supervisorctl
  supervisord
  tini
  uv
  node
  npm
  claude
  mngr
  system-interface
  ttyd
  cloudflared
  latchkey
  modal
  gh
  restic
  rg
  jq
  xxd
  sqlite3
  python3
)

for command_name in "${required_commands[@]}"; do
  command -v "$command_name" >/dev/null
done

echo "checking pinned/runtime versions"
test "${CLAUDE_CODE_VERSION:-}" = "2.1.160"
claude --version | grep -F "$CLAUDE_CODE_VERSION" >/dev/null

python3 - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit(f"expected Python >= 3.12, got {sys.version}")
PY

uv --version >/dev/null
node --version | grep -E "^v${FCT_EXPECTED_NODE_MAJOR}\." >/dev/null
npm --version >/dev/null
cloudflared --version >/dev/null
ttyd --version >/dev/null
gh --version >/dev/null
modal --version >/dev/null

echo "checking baked workspace layout"
test -f /etc/ssl/certs/ca-certificates.crt
test -x /usr/sbin/sshd
test -x /usr/local/bin/sftp-server
test -e /usr/lib/libstdc++.so.6
test -e /usr/lib/libgcc_s.so.1
test -e /var/run
test "$(grep '^root:' /etc/shadow | cut -d: -f2)" = "*"
mkdir -p /run/sshd
/usr/sbin/sshd -t
test -L /code
test "$(readlink /code)" = "/mngr/code"
test -L /worktree
test "$(readlink /worktree)" = "/mngr/worktree"
test -x /usr/local/bin/fct-seed
test -L /usr/local/bin/tk
test -L /usr/local/bin/ticket
if [ -n "${FCT_NIX_PROFILE:-}" ]; then
  test -s /etc/fct-workspace/nix-closure.txt
  grep -E '/nix/store/.+-nodejs-24\.16\.0' /etc/fct-workspace/nix-closure.txt >/dev/null
fi
test -d /docker_build_code
test -f /docker_build_code/pyproject.toml
test -f /docker_build_code/supervisord.conf
test -f /docker_build_code/apps/system_interface/imbue/system_interface/static/index.html
test ! -e /mngr/code || test -d /mngr/code
""",
    )

    volume_name = f"fct-image-contract-{os.getpid()}"
    _remove_volume(volume_name)
    _create_volume(volume_name)
    try:
        _run_in_image(
            r"""
set -euo pipefail
export PATH="/root/.local/bin:$PATH"
export OPENSSL_armcap=0
if [ -f /etc/profile.d/fct_path.sh ]; then
  . /etc/profile.d/fct_path.sh
fi

echo "seeding fresh /mngr volume"
/usr/local/bin/fct-seed

echo "checking seeded workspace layout"
test -d /mngr/code
test -f /mngr/code/pyproject.toml
test -f /mngr/code/supervisord.conf
test -d /mngr/worktree
test ! -e /docker_build_code
test -x /usr/local/bin/tk
test -x /usr/local/bin/ticket

# fct-seed must preserve an already-seeded workspace instead of overwriting it.
echo preserve-me > /mngr/code/.fct-seed-preserve-probe
/usr/local/bin/fct-seed
grep -F preserve-me /mngr/code/.fct-seed-preserve-probe >/dev/null

cd /mngr/code

test -d apps/system_interface/frontend/node_modules
test -f apps/system_interface/imbue/system_interface/static/index.html

echo "checking workspace Python imports"
uv run python - <<'PY'
import app_watcher.watcher
import bootstrap.manager
import cloudflare_tunnel.runner
import host_backup.runner
import imbue.mngr
import imbue.mngr_claude
import imbue.system_interface.main
import runtime_backup.runner
import web_server.runner
PY

echo "checking native Python wheels"
uv run python - <<'PY'
import gevent
import greenlet
import imbue.mngr.main
PY

echo "checking workspace CLI entry points"
uv run python scripts/forward_port.py --help >/dev/null
mngr --help >/dev/null
system-interface --help >/dev/null
mngr plugin list >/dev/null
tk --help >/dev/null
ticket --help >/dev/null

echo "checking supervisor can manage a process"
mkdir -p /var/log/supervisor
cat > /tmp/supervisord-smoke.conf <<'EOF'
[unix_http_server]
file=/tmp/fct-supervisor.sock
chmod=0700

[supervisorctl]
serverurl=unix:///tmp/fct-supervisor.sock

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisord]
nodaemon=false
logfile=/tmp/fct-supervisord.log
pidfile=/tmp/fct-supervisord.pid
childlogdir=/tmp

[program:probe]
command=/bin/sh -c "sleep 60"
autostart=true
autorestart=false
stdout_logfile=/tmp/fct-probe-stdout.log
stderr_logfile=/tmp/fct-probe-stderr.log
EOF

supervisord -c /tmp/supervisord-smoke.conf
for _ in $(seq 1 20); do
  if supervisorctl -c /tmp/supervisord-smoke.conf status probe | grep -F RUNNING >/dev/null; then
    supervisorctl -c /tmp/supervisord-smoke.conf shutdown >/dev/null
    exit 0
  fi
  sleep 0.25
done
supervisorctl -c /tmp/supervisord-smoke.conf status || true
supervisorctl -c /tmp/supervisord-smoke.conf shutdown >/dev/null || true
exit 1
""",
            volume_name=volume_name,
        )
    finally:
        _remove_volume(volume_name)


def test_fct_docker_provider_boot_contract(tmp_path: Path) -> None:
    if not _docker_provider_contract_enabled():
        pytest.skip("set FCT_DOCKER_PROVIDER_CONTRACT=1 to boot and smoke-test an FCT Docker workspace")
    if not _docker_available():
        pytest.skip("docker CLI/daemon is not available")

    prefix = f"fct-provider-{os.getpid()}-"
    host_name = "provider-smoke"
    agent_address = f"system-services@{host_name}.docker"
    project_config_dir = tmp_path / "project-config"
    _write_pytest_project_config(project_config_dir)
    env = _mngr_env(tmp_path, prefix, project_config_dir)
    created: dict[str, str] | None = None

    _remove_docker_resources_with_prefix(prefix)
    try:
        result = _run(
            [
                "uv",
                "run",
                "mngr",
                "create",
                agent_address,
                "--no-connect",
                "--format",
                "jsonl",
                "--new-host",
                "--template",
                "main",
                "--template",
                _PROVIDER_TEMPLATE,
            ],
            timeout=_BUILD_TIMEOUT_SECONDS + 900,
            check=True,
            env=env,
        )
        created = _created_event(result.stdout)
        with _connect_ssh(created) as client:
            stdout = _exec_ssh(
                client,
                "set -euo pipefail; "
                "pgrep -x sshd >/dev/null; "
                "case \"${LD_LIBRARY_PATH:-}\" in */nix/var/nix/profiles/fct-workspace/lib*) ;; *) exit 1 ;; esac; "
                "test -e /var/run; "
                "test -e /usr/lib/libstdc++.so.6; "
                "test -f /etc/fonts/fonts.conf; "
                "! fc-match sans 2>&1 | grep -q 'Fontconfig error'; "
                "test -d /mngr/code; "
                "test -f /mngr/code/supervisord.conf; "
                "command -v mngr >/dev/null; "
                "command -v system-interface >/dev/null; "
                "command -v sftp-server >/dev/null; "
                "cd /mngr/code; "
                "uv run python -c 'import gevent, greenlet, imbue.mngr.main'; "
                "for _ in $(seq 1 40); do "
                "  pgrep -f '[s]upervisord.*supervisord.conf' >/dev/null && break; "
                "  sleep 0.5; "
                "done; "
                "pgrep -f '[s]upervisord.*supervisord.conf' >/dev/null; "
                "for _ in $(seq 1 80); do "
                "  supervisorctl -c /mngr/code/supervisord.conf status system_interface web "
                "    | grep -E 'system_interface +RUNNING' >/dev/null "
                "  && supervisorctl -c /mngr/code/supervisord.conf status system_interface web "
                "    | grep -E 'web +RUNNING' >/dev/null "
                "  && break; "
                "  sleep 0.5; "
                "done; "
                "supervisorctl -c /mngr/code/supervisord.conf status system_interface web "
                "  | grep -E 'system_interface +RUNNING' >/dev/null; "
                "supervisorctl -c /mngr/code/supervisord.conf status system_interface web "
                "  | grep -E 'web +RUNNING' >/dev/null; "
                "for _ in $(seq 1 300); do "
                "  (supervisorctl -c /mngr/code/supervisord.conf status deferred-install || true) "
                "    | grep -E 'deferred-install +EXITED' >/dev/null "
                "  && break; "
                "  sleep 1; "
                "done; "
                "(supervisorctl -c /mngr/code/supervisord.conf status deferred-install || true) "
                "  | grep -E 'deferred-install +EXITED' >/dev/null; "
                "uv run python - <<'PY'\n"
                "from playwright.sync_api import sync_playwright\n"
                "with sync_playwright() as p:\n"
                "    browser = p.chromium.launch(headless=True)\n"
                "    page = browser.new_page()\n"
                "    page.goto('http://127.0.0.1:8000/', wait_until='domcontentloaded', timeout=15000)\n"
                "    assert page.title() == 'System Interface'\n"
                "    page.set_content('<h1>Hello Fontconfig</h1>')\n"
                "    assert page.locator('h1').inner_text() == 'Hello Fontconfig'\n"
                "    browser.close()\n"
                "PY\n"
                "printf fct-provider-ssh-ok",
            )
            assert stdout == "fct-provider-ssh-ok"

            with client.open_sftp() as sftp:
                remote_path = "/tmp/fct-provider-sftp-probe.txt"
                with sftp.file(remote_path, "wb") as remote_file:
                    remote_file.write(b"fct-provider-sftp-ok\n")
                with sftp.file(remote_path, "rb") as remote_file:
                    assert remote_file.read() == b"fct-provider-sftp-ok\n"
    finally:
        if created is not None:
            host_id = created.get("host_id")
            if host_id:
                _run(["docker", "image", "rm", "-f", f"mngr-build-{host_id}:latest"], timeout=60, check=False)
        _remove_docker_resources_with_prefix(prefix)
