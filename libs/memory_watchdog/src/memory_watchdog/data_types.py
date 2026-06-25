from datetime import datetime, timezone
from enum import auto
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from pydantic import Field

# The on-disk timestamp format for ledger records and the status file:
# nanosecond-precision ISO 8601 in UTC. Defined once here so producers and the
# strptime parser in watchdog._prune_recent_records cannot drift apart.
ISO_TIMESTAMP_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S.%f000Z"


def now_iso_timestamp() -> str:
    """Current UTC time as a nanosecond-precision ISO 8601 string.

    Delegates to imbue_common.format_nanosecond_iso_timestamp (the same helper
    mngr uses for event/discovery timestamps) so the watchdog does not reimplement
    the format.
    """
    return format_nanosecond_iso_timestamp(datetime.now(timezone.utc))


class Tier(UpperCaseStrEnum):
    """A process's OOM-priority tier, from most protected to most expendable."""

    # Tier 1: the tmux server, sshd, and the container entrypoint. Losing any of
    # these loses the user's ability to reach or recover the container.
    INFRASTRUCTURE = auto()
    # Tier 2: the user's window into the system -- the web UI, the tunnel that
    # carries it, and the terminal.
    USER_INTERFACE = auto()
    # Tier 3: the recovery machinery -- this watchdog. (The service manager,
    # supervisord, and the bootstrap launcher are tier-1 infrastructure.)
    RECOVERY = auto()
    # Tier 4: durability -- the runtime and host backup services.
    DURABILITY = auto()
    # Tier 5: agents the user created (the initial chat plus any New Agent /
    # New Chat ones). The shedder's last resort.
    USER_AGENT = auto()
    # Tier 6: auxiliary services -- telegram, web, app-watcher, and any service
    # an agent added to supervisord.conf.
    AUXILIARY_SERVICE = auto()
    # Tier 7: agents created by other agents (workers and the like).
    WORKER_AGENT = auto()
    # Tier 8: the build/test/browser subprocesses an agent spawns. Shed first.
    AGENT_CHILD = auto()


# Ordinal rank per tier (1 = most protected). Drives shed ordering and is the
# only place the 1..8 numbering is defined.
TIER_RANK_BY_TIER: Final[dict[Tier, int]] = {
    Tier.INFRASTRUCTURE: 1,
    Tier.USER_INTERFACE: 2,
    Tier.RECOVERY: 3,
    Tier.DURABILITY: 4,
    Tier.USER_AGENT: 5,
    Tier.AUXILIARY_SERVICE: 6,
    Tier.WORKER_AGENT: 7,
    Tier.AGENT_CHILD: 8,
}

# oom_score_adj written to /proc/<pid>/oom_score_adj per tier. Protected tiers
# (1-4) stay at 0; expendable tiers get increasingly positive values so the
# kernel's own OOM killer (where it honors per-process adjustment, i.e. runc)
# picks them first. Negative values would require CAP_SYS_RESOURCE, which the
# default Docker capability set does not grant, so positive-only tagging is used
# to achieve the same relative ordering without extra privileges.
OOM_SCORE_ADJ_BY_TIER: Final[dict[Tier, int]] = {
    Tier.INFRASTRUCTURE: 0,
    Tier.USER_INTERFACE: 0,
    Tier.RECOVERY: 0,
    Tier.DURABILITY: 0,
    Tier.USER_AGENT: 200,
    Tier.AUXILIARY_SERVICE: 400,
    Tier.WORKER_AGENT: 600,
    Tier.AGENT_CHILD: 900,
}

# Tiers the shedder may kill, ordered most-expendable first. Tiers 1-4 are never
# shed. USER_AGENT (5) is the last resort.
SHEDDABLE_TIERS_IN_SHED_ORDER: Final[tuple[Tier, ...]] = (
    Tier.AGENT_CHILD,
    Tier.WORKER_AGENT,
    Tier.AUXILIARY_SERVICE,
    Tier.USER_AGENT,
)


class ProcessInfo(FrozenModel):
    """A single process as observed in /proc at one instant."""

    pid: int = Field(description="Process id")
    parent_pid: int = Field(description="Parent process id (PPid from /proc)")
    resident_kb: int = Field(description="Resident set size in kibibytes")
    command_line: str = Field(
        description="Full argv joined by spaces, or comm when argv is empty"
    )


class TmuxPane(FrozenModel):
    """A tmux pane: the shell process that roots one window's process subtree."""

    session_name: str = Field(description="tmux session the pane belongs to")
    window_name: str = Field(
        description="tmux window name (e.g. bootstrap, 0)"
    )
    pane_pid: int = Field(description="PID of the pane's root shell process")


class ProcessClassification(FrozenModel):
    """The tier assigned to one process, with the reason for traceability."""

    pid: int = Field(description="Process id")
    resident_kb: int = Field(description="Resident set size in kibibytes")
    tier: Tier = Field(description="Assigned OOM-priority tier")
    # Human-readable label for what this process is (service name, agent name, or
    # a fallback). Used in the shed ledger so the user can see what was killed.
    label: str = Field(description="What this process is, for the ledger and banner")
    # The agent whose session this process lives under, set for an agent's own
    # process (tier 5/7) and for its subprocesses (tier 8) alike. Distinct from
    # ShedRecord.agent_name (which only marks a shed *agent* main process, for the
    # revival notice): this is for attribution/display -- "whose subprocess was
    # this". None for services and infrastructure.
    owning_agent_name: str | None = Field(
        default=None, description="Agent whose session this process belongs to, if any"
    )


class MemoryPressure(FrozenModel):
    """A point-in-time reading of container memory usage."""

    total_kb: int = Field(description="MemTotal in kibibytes")
    available_kb: int = Field(description="MemAvailable in kibibytes")

    @property
    def used_fraction(self) -> float:
        if self.total_kb <= 0:
            return 0.0
        return 1.0 - (self.available_kb / self.total_kb)


class ShedRecord(FrozenModel):
    """A single process the shedder killed, appended to the shed ledger."""

    timestamp: str = Field(
        description="Nanosecond-precision UTC ISO 8601 time of the kill"
    )
    tier: Tier = Field(description="Tier the process belonged to")
    tier_rank: int = Field(description="1..8 rank of the tier (8 = shed first)")
    label: str = Field(description="What the process was (service, agent, or command)")
    pid: int = Field(description="PID that was killed")
    resident_kb: int = Field(description="Resident memory reclaimed, in kibibytes")
    # Set when the killed process is an agent's own process (tier 5/7), so the
    # revival-notice hook can find which agents were shed.
    agent_name: str | None = Field(
        description="Agent whose main process this was, if any"
    )
    # The agent this process belonged to, set for an agent's subprocesses
    # (tier 8) as well as its main process -- for attribution in the banner
    # ("a subprocess of <agent>"). Unlike agent_name, this does NOT imply the
    # agent itself was shed, so it never triggers the revival notice.
    owning_agent_name: str | None = Field(
        default=None, description="Agent whose session this process belonged to, if any"
    )


class RecentShedSummary(FrozenModel):
    """An aggregated line for the UI banner: how many of one label were shed."""

    label: str = Field(description="Process label that was shed")
    tier_rank: int = Field(description="1..8 rank of the tier")
    count: int = Field(
        description="How many processes with this label were shed recently"
    )
    reclaimed_kb: int = Field(
        description="Total resident memory reclaimed for this label"
    )
    owning_agent_name: str | None = Field(
        default=None, description="Agent these processes belonged to, if any"
    )


class MemoryStatus(FrozenModel):
    """The watchdog's continuously published status, read by the UI banner."""

    timestamp: str = Field(description="When this status was written (UTC ISO 8601)")
    used_fraction: float = Field(description="Fraction of total memory in use, 0..1")
    total_kb: int = Field(description="MemTotal in kibibytes")
    available_kb: int = Field(description="MemAvailable in kibibytes")
    pressure_threshold_fraction: float = Field(
        description="Used-fraction at which shedding arms"
    )
    is_under_pressure: bool = Field(description="Whether the banner should be shown")
    recently_shed: tuple[RecentShedSummary, ...] = Field(
        description="What was shed recently"
    )
    blocked_services: tuple[str, ...] = Field(
        description="Services bootstrap has paused under pressure"
    )
