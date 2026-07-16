/**
 * Maps an agent's mngr lifecycle state to the liveness category shown by the
 * per-agent dot on its chat tab. The dot is a glanceable summary of the three
 * things a user cares about at a glance, each with its own color:
 *
 *   - "active"  -> the claude process is up and working      (green)
 *   - "waiting" -> the process is up but idle, waiting on you (yellow)
 *   - "dormant" -> the process isn't running                 (grey)
 *
 * This is distinct from the chat's activity indicator (THINKING / TOOL_RUNNING /
 * IDLE), which only describes work *within* a running process. The liveness dot
 * describes the process itself.
 *
 * In this all-local deployment every non-running state is equally recoverable --
 * DONE (claude exited to a shell), STOPPED (the tmux window is gone), REPLACED,
 * UNKNOWN -- because sending the agent a message revives it. So they all read as
 * "dormant" rather than as an error; the dot has no "dead"/red category.
 */
export type AgentLivenessCategory = "active" | "waiting" | "dormant";

// Lifecycle states in which the claude process is up and actively working.
// RUNNING_UNKNOWN_AGENT_TYPE is an agent running under a process name mngr
// can't confirm; we treat it as running rather than dormant.
const ACTIVE_STATES: ReadonlySet<string> = new Set(["RUNNING", "RUNNING_UNKNOWN_AGENT_TYPE"]);

// Lifecycle states in which the claude process is alive, whether working
// (RUNNING) or idle (WAITING). Outside this set the process is not running.
const ALIVE_STATES: ReadonlySet<string> = new Set(["RUNNING", "RUNNING_UNKNOWN_AGENT_TYPE", "WAITING"]);

// Activity states that mean the agent is mid-turn (see ActivityIndicator).
const WORKING_ACTIVITY_STATES: ReadonlySet<string> = new Set(["THINKING", "TOOL_RUNNING"]);

export function livenessCategoryForState(state: string): AgentLivenessCategory {
  if (ACTIVE_STATES.has(state)) {
    return "active";
  }
  if (state === "WAITING") {
    return "waiting";
  }
  return "dormant";
}

/**
 * Resolve the lifecycle state to actually display on the dot, using the fast
 * local activity signal to decide active-vs-idle among live agents.
 *
 * The lifecycle RUNNING/WAITING split itself comes only from the system
 * interface's lifecycle poll, which lags a sent message by up to one poll
 * interval -- so a just-messaged WAITING agent would stay yellow for that whole
 * window. The activity signal (``activity_state``, plus the optimistic
 * forced-THINKING the send applies) updates promptly and answers the same
 * working-vs-idle question, so among live agents we let it drive the color:
 *
 *   - not a live state -> returned unchanged (dormant: DONE/STOPPED/REPLACED/UNKNOWN)
 *   - live, activity not tracked (null) -> returned unchanged (trust lifecycle)
 *   - live, activity working -> "RUNNING"
 *   - live, activity idle -> "WAITING"
 *
 * The result is fed to ``livenessCategoryForState`` for the color and shown
 * verbatim in the hover tooltip, so the two never disagree.
 */
export function effectiveLifecycleState(state: string, activity: string | null): string {
  if (!ALIVE_STATES.has(state)) {
    return state;
  }
  if (activity === null) {
    return state;
  }
  return WORKING_ACTIVITY_STATES.has(activity) ? "RUNNING" : "WAITING";
}
