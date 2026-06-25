import { describe, expect, it } from "vitest";
import {
  creatorLabel,
  humanizeKb,
  pausedSummary,
  processName,
  tierLabel,
  totalPausedCount,
} from "./MemoryPressureBanner";

function item(
  label: string,
  tier_rank: number,
  count: number,
  reclaimed_kb: number,
  owning_agent_name: string | null = null,
) {
  return { label, tier_rank, count, reclaimed_kb, owning_agent_name };
}

describe("humanizeKb", () => {
  it("renders KB below 1 MB", () => {
    expect(humanizeKb(512)).toBe("512 KB");
  });
  it("renders whole MB between 1 MB and 1 GB", () => {
    expect(humanizeKb(2048)).toBe("2 MB");
    expect(humanizeKb(500_000)).toBe("488 MB");
  });
  it("renders GB with one decimal at/above 1 GB", () => {
    expect(humanizeKb(3_817_068)).toBe("3.6 GB");
  });
});

describe("tierLabel", () => {
  it("maps the sheddable tiers to friendly names", () => {
    expect(tierLabel(8)).toBe("Agent subprocess");
    expect(tierLabel(7)).toBe("Worker agent");
    expect(tierLabel(6)).toBe("Background service");
    expect(tierLabel(5)).toBe("Agent");
  });
  it("falls back for an unknown rank", () => {
    expect(tierLabel(1)).toBe("Background process");
  });
});

describe("paused count + summary", () => {
  it("sums counts across items", () => {
    expect(totalPausedCount([item("python3", 8, 1, 100), item("sleep", 8, 3, 30)])).toBe(4);
  });
  it("returns null when nothing was paused (toggle hidden)", () => {
    expect(pausedSummary([])).toBe(null);
  });
  it("uses singular for one and plural for many", () => {
    expect(pausedSummary([item("python3", 8, 1, 100)])).toBe("1 background task paused");
    expect(pausedSummary([item("python3", 8, 2, 100)])).toBe("2 background tasks paused");
  });
});

describe("processName", () => {
  it("shows the command alone for a single process", () => {
    expect(processName(item("python3 hog.py", 8, 1, 3_131_972))).toBe("python3 hog.py");
  });
  it("appends a multiplier when several of one kind were paused", () => {
    expect(processName(item("sleep", 8, 3, 3324))).toBe("sleep ×3");
  });
});

describe("creatorLabel", () => {
  it("names the owning agent for a subprocess", () => {
    expect(creatorLabel(item("python3 hog.py", 8, 1, 3_131_972, "hogtest"))).toBe("hogtest");
  });
  it("falls back to a friendly kind when a subprocess has no owner", () => {
    expect(creatorLabel(item("python3", 8, 1, 1000))).toBe("Agent");
  });
  it("describes the kind for non-subprocess tiers", () => {
    expect(creatorLabel(item("alice", 5, 1, 90000, "alice"))).toBe("Agent");
    expect(creatorLabel(item("worker", 7, 1, 90000, "worker"))).toBe("Worker agent");
    expect(creatorLabel(item("web", 6, 1, 90000))).toBe("Background service");
  });
});
