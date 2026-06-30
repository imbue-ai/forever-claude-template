import { describe, expect, it } from "vitest";
import { livenessCategoryForState } from "./agentLiveness";

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
