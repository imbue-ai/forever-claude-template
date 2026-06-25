import { describe, expect, it } from "vitest";
import { humanizeKb, pausedSummary, shedItemDetail, tierLabel, totalPausedCount } from "./MemoryPressureBanner";

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

describe("shedItemDetail", () => {
  it("omits the multiplier for a single process", () => {
    expect(shedItemDetail(item("python3", 8, 1, 3_817_068))).toEqual({
      name: "python3",
      meta: "Agent subprocess · 3.6 GB freed",
    });
  });
  it("shows the multiplier and tier for several of one kind", () => {
    expect(shedItemDetail(item("sleep", 8, 3, 3324))).toEqual({
      name: "sleep ×3",
      meta: "Agent subprocess · 3 MB freed",
    });
  });

  it("attributes an agent subprocess to its owning agent", () => {
    expect(shedItemDetail(item("python3 hog.py", 8, 1, 3_131_972, "alice"))).toEqual({
      name: "python3 hog.py",
      meta: "Agent subprocess from alice · 3.0 GB freed",
    });
  });

  it("does not append 'from <agent>' for the agent's own process", () => {
    // A tier-5 agent line is already named by its label; no redundant "from".
    expect(shedItemDetail(item("alice", 5, 1, 90000, "alice"))).toEqual({
      name: "alice",
      meta: "Agent · 88 MB freed",
    });
  });
});
