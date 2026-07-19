import { describe, expect, it } from "vitest";
import { isWorkingActivityState, labelForActivityState } from "./ActivityIndicator";

// The TOOL_RUNNING caption is now computed on the backend (activity_caption.py);
// this component is a pure renderer over {state, caption}. The transcript-
// enrichment cases that used to live here moved to activity_caption_test.py.

describe("labelForActivityState", () => {
  it("returns null for null/undefined state (no tracking)", () => {
    expect(labelForActivityState(null, null)).toBe(null);
    expect(labelForActivityState(undefined, null)).toBe(null);
  });

  it("returns null for IDLE (collapsed strip)", () => {
    expect(labelForActivityState("IDLE", null)).toBe(null);
  });

  it("returns null for an unknown/future state", () => {
    expect(labelForActivityState("SOMETHING_NEW", "ignored")).toBe(null);
  });

  it("returns a fixed label for THINKING, ignoring any caption", () => {
    expect(labelForActivityState("THINKING", null)).toBe("Thinking…");
    expect(labelForActivityState("THINKING", "Editing foo.py")).toBe("Thinking…");
  });

  it("shows the server caption for TOOL_RUNNING", () => {
    expect(labelForActivityState("TOOL_RUNNING", "Editing foo.py")).toBe("Editing foo.py");
    expect(labelForActivityState("TOOL_RUNNING", "Running code")).toBe("Running code");
  });

  it("falls back to a generic label when TOOL_RUNNING has no caption", () => {
    expect(labelForActivityState("TOOL_RUNNING", null)).toBe("Running tool…");
    expect(labelForActivityState("TOOL_RUNNING", undefined)).toBe("Running tool…");
  });
});

describe("isWorkingActivityState — stop-button visibility gate", () => {
  it("is true for THINKING and TOOL_RUNNING", () => {
    expect(isWorkingActivityState("THINKING")).toBe(true);
    expect(isWorkingActivityState("TOOL_RUNNING")).toBe(true);
  });

  it("is false for IDLE, null, undefined, and unknown", () => {
    expect(isWorkingActivityState("IDLE")).toBe(false);
    expect(isWorkingActivityState(null)).toBe(false);
    expect(isWorkingActivityState(undefined)).toBe(false);
    expect(isWorkingActivityState("SOMETHING_NEW")).toBe(false);
  });
});
