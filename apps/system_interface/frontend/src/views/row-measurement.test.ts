import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mithril is mocked: redraw is a spy so we can assert on the measure->redraw
// scheduling without a real DOM or render cycle.
const { mockRedraw } = vi.hoisted(() => ({ mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({
  default: { redraw: mockRedraw },
}));

import { createRowMeasurer, MEASURE_HYSTERESIS_PX } from "./row-measurement";

/** A fake rendered row whose measured (sub-pixel) height we can mutate per-frame. */
interface FakeRow {
  element: HTMLElement;
  setHeight: (height: number) => void;
}

function fakeRow(id: string, height: number): FakeRow {
  let current = height;
  const element = {
    id,
    getBoundingClientRect: () => ({ height: current }) as DOMRect,
  } as unknown as HTMLElement;
  return { element, setHeight: (h) => (current = h) };
}

/** A fake scroll container whose ``.message-list`` holds the given rows. */
function fakeScrollEl(rows: FakeRow[]): HTMLElement {
  const list = { children: rows.map((r) => r.element) } as unknown as Element;
  return {
    querySelector: (selector: string) => (selector === ".message-list" ? list : null),
  } as unknown as HTMLElement;
}

beforeEach(() => {
  mockRedraw.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("measureRows", () => {
  it("caches heights and reports a change on first measurement", () => {
    const measurer = createRowMeasurer();
    const rows = [fakeRow("a", 100), fakeRow("b", 200)];

    expect(measurer.measureRows(fakeScrollEl(rows))).toBe(true);
    expect(measurer.getHeight("a")).toBe(100);
    expect(measurer.getHeight("b")).toBe(200);
  });

  it("reports no change when nothing moved", () => {
    const measurer = createRowMeasurer();
    const rows = [fakeRow("a", 100)];
    measurer.measureRows(fakeScrollEl(rows));

    expect(measurer.measureRows(fakeScrollEl(rows))).toBe(false);
  });

  it("ignores a sub-pixel change without updating the cache (the jitter fix)", () => {
    // This is the exact loop we are breaking: a row drifting a fraction of a pixel
    // must not be read as a change, or it would schedule a redraw that shifts it
    // again forever.
    const measurer = createRowMeasurer();
    const row = fakeRow("a", 100);
    measurer.measureRows(fakeScrollEl([row]));

    row.setHeight(100.4);
    expect(measurer.measureRows(fakeScrollEl([row]))).toBe(false);
    // The cache stays anchored to the original stable height, not the drifted one.
    expect(measurer.getHeight("a")).toBe(100);
  });

  it("ignores a change of exactly the threshold, but reports one just past it", () => {
    const measurer = createRowMeasurer();
    const row = fakeRow("a", 100);
    measurer.measureRows(fakeScrollEl([row]));

    // A delta equal to the threshold is not "more than" the threshold: ignored.
    row.setHeight(100 + MEASURE_HYSTERESIS_PX);
    expect(measurer.measureRows(fakeScrollEl([row]))).toBe(false);
    expect(measurer.getHeight("a")).toBe(100);

    // Just past the threshold counts.
    row.setHeight(100 + MEASURE_HYSTERESIS_PX + 0.01);
    expect(measurer.measureRows(fakeScrollEl([row]))).toBe(true);
    expect(measurer.getHeight("a")).toBe(100 + MEASURE_HYSTERESIS_PX + 0.01);
  });

  it("reports a genuine height change and updates the cache", () => {
    // A streamed line adds well more than a pixel, so real growth still settles.
    const measurer = createRowMeasurer();
    const row = fakeRow("a", 100);
    measurer.measureRows(fakeScrollEl([row]));

    row.setHeight(122);
    expect(measurer.measureRows(fakeScrollEl([row]))).toBe(true);
    expect(measurer.getHeight("a")).toBe(122);
  });

  it("does not accumulate repeated sub-threshold drift into a change", () => {
    // Because sub-threshold deltas leave the cache anchored, a row that keeps
    // wobbling within +/-1px of its true height never ratchets across the
    // threshold -- the loop stays broken frame after frame.
    const measurer = createRowMeasurer();
    const row = fakeRow("a", 100);
    measurer.measureRows(fakeScrollEl([row]));

    for (const wobble of [100.6, 99.5, 100.7, 99.6, 100.8]) {
      row.setHeight(wobble);
      expect(measurer.measureRows(fakeScrollEl([row]))).toBe(false);
    }
    expect(measurer.getHeight("a")).toBe(100);
  });

  it("skips spacer children (empty id) and unlaid-out rows (zero height)", () => {
    const measurer = createRowMeasurer();
    const rows = [fakeRow("", 500), fakeRow("a", 0), fakeRow("b", 150)];

    expect(measurer.measureRows(fakeScrollEl(rows))).toBe(true);
    expect(measurer.getHeight("a")).toBeUndefined();
    expect(measurer.getHeight("b")).toBe(150);
  });

  it("returns false when there is no message list", () => {
    const measurer = createRowMeasurer();
    const scrollEl = { querySelector: () => null } as unknown as HTMLElement;

    expect(measurer.measureRows(scrollEl)).toBe(false);
  });
});

describe("scheduleMeasure", () => {
  let frames: Array<() => void>;

  beforeEach(() => {
    frames = [];
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      frames.push(() => cb(0));
      return frames.length;
    });
  });

  it("redraws once on the next frame when a height changed", () => {
    const measurer = createRowMeasurer();
    const rows = [fakeRow("a", 100)];

    measurer.scheduleMeasure(() => fakeScrollEl(rows));
    expect(mockRedraw).not.toHaveBeenCalled(); // deferred to the frame
    frames.forEach((run) => run());
    expect(mockRedraw).toHaveBeenCalledTimes(1);
  });

  it("does not redraw when the measurement is stable", () => {
    const measurer = createRowMeasurer();
    const rows = [fakeRow("a", 100)];
    measurer.measureRows(fakeScrollEl(rows)); // prime the cache

    measurer.scheduleMeasure(() => fakeScrollEl(rows));
    frames.forEach((run) => run());
    expect(mockRedraw).not.toHaveBeenCalled();
  });

  it("does not redraw when a sub-threshold drift schedules a measure", () => {
    // The end-to-end guarantee: even if the view keeps scheduling measures, a
    // row wobbling sub-pixel never triggers the redraw that would move it.
    const measurer = createRowMeasurer();
    const row = fakeRow("a", 100);
    measurer.measureRows(fakeScrollEl([row]));

    row.setHeight(100.5);
    measurer.scheduleMeasure(() => fakeScrollEl([row]));
    frames.forEach((run) => run());
    expect(mockRedraw).not.toHaveBeenCalled();
  });

  it("debounces multiple calls into a single frame", () => {
    const measurer = createRowMeasurer();
    const rows = [fakeRow("a", 100)];

    measurer.scheduleMeasure(() => fakeScrollEl(rows));
    measurer.scheduleMeasure(() => fakeScrollEl(rows));
    measurer.scheduleMeasure(() => fakeScrollEl(rows));
    expect(frames).toHaveLength(1);
  });

  it("does not redraw when the scroll element is gone", () => {
    const measurer = createRowMeasurer();

    measurer.scheduleMeasure(() => null);
    frames.forEach((run) => run());
    expect(mockRedraw).not.toHaveBeenCalled();
  });
});

describe("prune and reset", () => {
  it("drops stale keys only once the cache drifts well past the live rows", () => {
    const measurer = createRowMeasurer();
    const rows = Array.from({ length: 300 }, (_, i) => fakeRow(`row-${i}`, 100));
    measurer.measureRows(fakeScrollEl(rows));

    // 300 cached, 250 live: 300 <= 250 + 256, so we are under the slack and
    // nothing is pruned (avoids churn on small evictions).
    const manyLive = new Set(Array.from({ length: 250 }, (_, i) => `row-${i}`));
    measurer.prune(manyLive);
    expect(measurer.getHeight("row-299")).toBe(100);

    // 300 cached, 1 live: 300 > 1 + 256, so the cache is over budget and stale
    // keys are dropped while the live one is kept.
    measurer.prune(new Set(["row-0"]));
    expect(measurer.getHeight("row-0")).toBe(100);
    expect(measurer.getHeight("row-299")).toBeUndefined();
  });

  it("forgets all heights on reset", () => {
    const measurer = createRowMeasurer();
    measurer.measureRows(fakeScrollEl([fakeRow("a", 100)]));
    measurer.reset();

    expect(measurer.getHeight("a")).toBeUndefined();
  });
});
