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

export function livenessCategoryForState(state: string): AgentLivenessCategory {
  if (ACTIVE_STATES.has(state)) {
    return "active";
  }
  if (state === "WAITING") {
    return "waiting";
  }
  return "dormant";
}
