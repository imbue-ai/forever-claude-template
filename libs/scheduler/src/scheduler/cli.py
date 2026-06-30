"""The ``scheduler`` console script: a Click group.

``scheduler run`` is the daemon (run by supervisord). The remaining commands let
agents and users query and edit runtime/scheduled_tasks.toml in a consistent,
validated way (see the ``manage-scheduled-tasks`` skill).
"""

import json

import click
from loguru import logger

from scheduler.data_types import ScheduledTask
from scheduler.errors import SchedulerError
from scheduler.runner import run_loop
from scheduler.schedule_file import add_task, read_schedule, remove_task


@click.group()
def main() -> None:
    """File-driven task scheduler with offline catch-up."""


@main.command()
def run() -> None:
    """Run the scheduler daemon (used by supervisord)."""
    run_loop()


@main.command(name="list")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["human", "json"]),
    default="human",
    help="Output format.",
)
def list_tasks(output_format: str) -> None:
    """List the currently scheduled tasks."""
    tasks = read_schedule()
    if output_format == "json":
        click.echo(json.dumps([task.model_dump() for task in tasks], indent=2))
        return
    if not tasks:
        click.echo("No scheduled tasks.")
        return
    for task in tasks:
        status = "enabled" if task.enabled else "disabled"
        catch_up = "catch-up" if task.catch_up else "no-catch-up"
        click.echo(
            f"{task.name}\t{task.schedule}\t{status}\t{catch_up}\t{task.command}"
        )
        if task.description:
            click.echo(f"    {task.description}")


@main.command()
@click.option("--name", required=True, help="Unique task name.")
@click.option(
    "--schedule", "schedule", required=True, help="Standard 5-field cron expression."
)
@click.option(
    "--command",
    "command",
    required=True,
    help="Shell command to run from the repo root.",
)
@click.option("--description", default="", help="Human-readable note about the task.")
@click.option("--disabled", is_flag=True, help="Create the task disabled.")
@click.option(
    "--no-catch-up", is_flag=True, help="Do not run missed fire times on boot."
)
@click.option(
    "--replace", is_flag=True, help="Overwrite an existing task with the same name."
)
def add(
    name: str,
    schedule: str,
    command: str,
    description: str,
    disabled: bool,
    no_catch_up: bool,
    replace: bool,
) -> None:
    """Add a task to the schedule."""
    task = ScheduledTask(
        name=name,
        schedule=schedule,
        command=command,
        description=description,
        enabled=not disabled,
        catch_up=not no_catch_up,
    )
    try:
        add_task(task, replace=replace)
    except SchedulerError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"Added task {name!r}.")


@main.command()
@click.argument("name")
def remove(name: str) -> None:
    """Remove a task from the schedule."""
    removed = remove_task(name)
    if removed:
        click.echo(f"Removed task {name!r}.")
    else:
        click.echo(f"No task named {name!r}.")


@main.command()
@click.argument("name")
def show(name: str) -> None:
    """Show a single task as JSON."""
    for task in read_schedule():
        if task.name == name:
            click.echo(json.dumps(task.model_dump(), indent=2))
            return
    raise click.ClickException(f"No task named {name!r}.")


if __name__ == "__main__":
    logger.disable("scheduler")
    main()
