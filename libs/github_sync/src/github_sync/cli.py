"""CLI for github-sync: the service loop plus the helpers the skill drives.

`uv run github-sync run` is what the [program:github-sync] supervisord block
executes; the remaining subcommands are one-shot steps invoked by the
github-sync skill during enable / disable / status.
"""

import json

import click

from github_sync import runner
from github_sync.config import GithubSyncConfigError, load_repo_url
from github_sync.visibility import VISIBILITY_PRIVATE, check_repo_visibility
from github_sync.wiring import apply_git_wiring, remove_git_wiring
from github_sync.worktree import init_runtime_worktree, is_runtime_worktree


@click.group()
def main() -> None:
    """Manage the opt-in GitHub sync for this workspace."""


@main.command()
def run() -> None:
    """Run the periodic runtime/ sync service loop (used by supervisord)."""
    runner.run_forever()


@main.command("wire-git")
def wire_git() -> None:
    """Route git's GitHub access through the latchkey gateway (global config)."""
    if not apply_git_wiring():
        raise SystemExit(1)
    click.echo("wired")


@main.command("unwire-git")
def unwire_git() -> None:
    """Remove the gateway git config and the hooks path (disable path)."""
    remove_git_wiring()
    click.echo("unwired")


@main.command("setup-worktree")
def setup_worktree() -> None:
    """Create runtime/ as a worktree of runtime-sync, restoring from origin if it exists there."""
    if not init_runtime_worktree():
        raise SystemExit(1)
    click.echo("ready")


@main.command("check-visibility")
def check_visibility() -> None:
    """Print the sync repo's visibility; exits nonzero unless confirmed private."""
    try:
        repo_url = load_repo_url()
    except GithubSyncConfigError as e:
        raise click.ClickException(str(e)) from e
    if repo_url is None:
        raise click.ClickException("sync is not configured (github_sync.toml missing)")
    visibility = check_repo_visibility(repo_url)
    click.echo(visibility)
    if visibility != VISIBILITY_PRIVATE:
        raise SystemExit(1)


@main.command()
def status() -> None:
    """Print the sync configuration and the service's latest status as JSON."""
    try:
        repo_url = load_repo_url()
        config_error = None
    except GithubSyncConfigError as e:
        repo_url = None
        config_error = str(e)
    service_status = None
    status_path = runner.status_file_path()
    if status_path.exists():
        try:
            service_status = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            service_status = {"error": f"unreadable status file: {e}"}
    payload = {
        "is_configured": repo_url is not None,
        "repo_url": repo_url,
        "config_error": config_error,
        "is_runtime_worktree": is_runtime_worktree(),
        "service": service_status,
    }
    click.echo(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
