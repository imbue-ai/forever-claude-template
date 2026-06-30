import { describe, expect, it } from "vitest";
import { effectiveLifecycleState, livenessCategoryForState } from "./agentLiveness";

describe("livenessCategoryForState", () => {
  it("maps the working states to active", () => {
    expect(livenessCategoryForState("RUNNING")).toBe("active");
    expect(livenessCategoryForState("RUNNING_UNKNOWN_AGENT_TYPE")).toBe("active");
  });

  it("maps the idle-but-up state to waiting", () => {
    expect(livenessCategoryForState("WAITING")).toBe("waiting");
  });

  it("maps every non-running state to dormant", () => {
    // In this all-local deployment these are all recoverable (revived on the
    // next message), so they share the single dormant (grey) category rather
    // than distinguishing a "dead" state.
    expect(livenessCategoryForState("DONE")).toBe("dormant");
    expect(livenessCategoryForState("STOPPED")).toBe("dormant");
    expect(livenessCategoryForState("REPLACED")).toBe("dormant");
    expect(livenessCategoryForState("UNKNOWN")).toBe("dormant");
  });

  it("treats an unrecognized state as dormant rather than throwing", () => {
    expect(livenessCategoryForState("SOMETHING_NEW")).toBe("dormant");
  });
});

describe("effectiveLifecycleState", () => {
  it("lets prompt activity drive active-vs-idle among live agents", () => {
    // A just-messaged WAITING agent: lifecycle still says WAITING (the poll
    // lags), but the activity signal already reads THINKING -- so the dot should
    // resolve to RUNNING (green) immediately rather than staying yellow.
    expect(effectiveLifecycleState("WAITING", "THINKING")).toBe("RUNNING");
    expect(effectiveLifecycleState("WAITING", "TOOL_RUNNING")).toBe("RUNNING");
    // A finished agent whose lifecycle still says RUNNING resolves to WAITING.
    expect(effectiveLifecycleState("RUNNING", "IDLE")).toBe("WAITING");
  });

  it("passes dormant states through unchanged regardless of activity", () => {
    expect(effectiveLifecycleState("DONE", "THINKING")).toBe("DONE");
    expect(effectiveLifecycleState("STOPPED", "IDLE")).toBe("STOPPED");
    expect(effectiveLifecycleState("REPLACED", null)).toBe("REPLACED");
    expect(effectiveLifecycleState("UNKNOWN", "TOOL_RUNNING")).toBe("UNKNOWN");
  });

  it("trusts the lifecycle state when activity is not tracked", () => {
    // No activity tracking (e.g. a remote or non-claude agent): fall back to the
    // lifecycle state rather than forcing it to look idle.
    expect(effectiveLifecycleState("RUNNING", null)).toBe("RUNNING");
    expect(effectiveLifecycleState("WAITING", null)).toBe("WAITING");
    expect(effectiveLifecycleState("RUNNING_UNKNOWN_AGENT_TYPE", null)).toBe("RUNNING_UNKNOWN_AGENT_TYPE");
  });

  it("composes with livenessCategoryForState to color the dot", () => {
    expect(livenessCategoryForState(effectiveLifecycleState("WAITING", "THINKING"))).toBe("active");
    expect(livenessCategoryForState(effectiveLifecycleState("RUNNING", "IDLE"))).toBe("waiting");
    expect(livenessCategoryForState(effectiveLifecycleState("DONE", "THINKING"))).toBe("dormant");
  });
});
