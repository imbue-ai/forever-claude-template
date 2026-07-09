import { describe, expect, it } from "vitest";
import { computeVisibleWindow, computeTranscriptSlices, type WindowSegment } from "./virtualWindow";

const uniform =
  (height: number) =>
  (_index: number): number =>
    height;

/** Every row index the segments would render, in order. */
function renderedIndices(segments: WindowSegment[]): number[] {
  const indices: number[] = [];
  for (const segment of segments) {
    if (segment.kind === "rows") {
      for (let i = segment.startIndex; i < segment.endIndex; i++) indices.push(i);
    }
  }
  return indices;
}

/** Total height the segments occupy (spacers + rendered rows). */
function segmentsHeight(segments: WindowSegment[], getHeight: (i: number) => number): number {
  let height = 0;
  for (const segment of segments) {
    if (segment.kind === "spacer") height += segment.height;
    else for (let i = segment.startIndex; i < segment.endIndex; i++) height += getHeight(i);
  }
  return height;
}

describe("computeVisibleWindow", () => {
  it("returns an empty window with no padding for an empty list", () => {
    const result = computeVisibleWindow({
      count: 0,
      getHeight: uniform(100),
      scrollTop: 0,
      viewportHeight: 500,
      overscanPx: 0,
    });
    expect(result).toEqual({ startIndex: 0, endIndex: 0, topPad: 0, bottomPad: 0, totalHeight: 0 });
  });

  it("renders only the viewport slice with spacers summing to the total height", () => {
    // 100 rows of 100px = 10000px tall; viewport 500px at the top, no overscan.
    const result = computeVisibleWindow({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 0,
      viewportHeight: 500,
      overscanPx: 0,
    });
    expect(result.startIndex).toBe(0);
    // rows 0..4 fully cover 0..500; row 5 starts exactly at 500 (not < 500).
    expect(result.endIndex).toBe(5);
    expect(result.topPad).toBe(0);
    expect(result.bottomPad).toBe(100 * 100 - 500);
    expect(result.totalHeight).toBe(10000);
    // Spacers + rendered rows always reconstruct the full height.
    const renderedHeight = (result.endIndex - result.startIndex) * 100;
    expect(result.topPad + renderedHeight + result.bottomPad).toBe(result.totalHeight);
  });

  it("windows around a mid-list scroll position", () => {
    const result = computeVisibleWindow({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 5000,
      viewportHeight: 500,
      overscanPx: 0,
    });
    // Viewport covers 5000..5500 -> rows 50..54.
    expect(result.startIndex).toBe(50);
    expect(result.endIndex).toBe(55);
    expect(result.topPad).toBe(5000);
    expect(result.bottomPad).toBe(10000 - 5500);
  });

  it("expands the window by the overscan margin on both sides", () => {
    const noOverscan = computeVisibleWindow({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 5000,
      viewportHeight: 500,
      overscanPx: 0,
    });
    const withOverscan = computeVisibleWindow({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 5000,
      viewportHeight: 500,
      overscanPx: 200,
    });
    expect(withOverscan.startIndex).toBeLessThan(noOverscan.startIndex);
    expect(withOverscan.endIndex).toBeGreaterThan(noOverscan.endIndex);
  });

  it("handles variable row heights", () => {
    // Heights: row i is (i + 1) * 10 px. Cumulative offset of row k is
    // 10 * (1 + 2 + ... + k) = 5 * k * (k + 1).
    const heights = (i: number) => (i + 1) * 10;
    const result = computeVisibleWindow({
      count: 20,
      getHeight: heights,
      scrollTop: 100,
      viewportHeight: 50,
      overscanPx: 0,
    });
    // Reconstruct total and verify the pads bracket the rendered rows exactly.
    let total = 0;
    for (let i = 0; i < 20; i++) total += heights(i);
    let rendered = 0;
    for (let i = result.startIndex; i < result.endIndex; i++) rendered += heights(i);
    expect(result.topPad + rendered + result.bottomPad).toBe(total);
    expect(result.totalHeight).toBe(total);
    // The first rendered row must straddle or follow scrollTop=100; the row
    // before it must end at or before 100.
    let offsetBeforeStart = 0;
    for (let i = 0; i < result.startIndex; i++) offsetBeforeStart += heights(i);
    expect(offsetBeforeStart).toBeLessThanOrEqual(100);
  });

  it("fills backward to cover the viewport when scrolled past the end", () => {
    const result = computeVisibleWindow({
      count: 10,
      getHeight: uniform(100),
      scrollTop: 100000,
      viewportHeight: 500,
      overscanPx: 0,
    });
    // Coverage = viewport (500) + 2*overscan (0) = 500px -> the last 5 rows.
    expect(result.startIndex).toBe(5);
    expect(result.endIndex).toBe(10);
    expect(result.bottomPad).toBe(0);
    expect(result.topPad).toBe(500);
    const rendered = (result.endIndex - result.startIndex) * 100;
    expect(result.topPad + rendered + result.bottomPad).toBe(result.totalHeight);
  });

  it("includes overscan in the past-the-end backward fill", () => {
    const result = computeVisibleWindow({
      count: 10,
      getHeight: uniform(100),
      scrollTop: 100000,
      viewportHeight: 500,
      overscanPx: 200,
    });
    // Coverage = 500 + 2*200 = 900px -> the last 9 rows.
    expect(result.startIndex).toBe(1);
    expect(result.endIndex).toBe(10);
    expect(result.bottomPad).toBe(0);
  });
});

describe("computeTranscriptSlices", () => {
  it("renders one contiguous run matching the viewport when there is no pin", () => {
    const { segments, totalHeight } = computeTranscriptSlices({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 5000,
      viewportHeight: 500,
      overscanPx: 0,
    });
    // spacer, one row-run, spacer.
    expect(segments.map((s) => s.kind)).toEqual(["spacer", "rows", "spacer"]);
    expect(renderedIndices(segments)).toEqual([50, 51, 52, 53, 54]);
    expect(totalHeight).toBe(10000);
    expect(segmentsHeight(segments, uniform(100))).toBe(totalHeight);
  });

  it("merges a pin that overlaps or touches the viewport into one run", () => {
    const { segments } = computeTranscriptSlices({
      count: 100,
      getHeight: uniform(100),
      scrollTop: 5000,
      viewportHeight: 500,
      overscanPx: 0,
      pinnedRange: { start: 55, end: 56 }, // adjacent to the viewport window [50,55)
    });
    expect(segments.map((s) => s.kind)).toEqual(["spacer", "rows", "spacer"]);
    expect(renderedIndices(segments)).toEqual([50, 51, 52, 53, 54, 55, 56]);
  });

  it("keeps a far-above selection as a SEPARATE run -- not the rows in between", () => {
    // The whole point of the disjoint window: a selection at the top with the
    // viewport near the bottom must NOT mount the ~900 rows between them.
    const { segments } = computeTranscriptSlices({
      count: 1000,
      getHeight: uniform(100),
      scrollTop: 95000, // viewport near the bottom -> rows ~950..954
      viewportHeight: 500,
      overscanPx: 0,
      pinnedRange: { start: 2, end: 3 },
    });
    expect(segments.map((s) => s.kind)).toEqual(["spacer", "rows", "spacer", "rows", "spacer"]);
    const indices = renderedIndices(segments);
    // Only the 2 pinned rows plus the ~5 viewport rows -- a handful, not hundreds.
    expect(indices).toContain(2);
    expect(indices).toContain(3);
    expect(indices).toContain(950);
    expect(indices).not.toContain(500); // nothing from the gap between
    expect(indices.length).toBeLessThan(20);
    expect(segmentsHeight(segments, uniform(100))).toBe(100000);
  });

  it("keeps a far-below selection as a separate run after the viewport", () => {
    const { segments } = computeTranscriptSlices({
      count: 1000,
      getHeight: uniform(100),
      scrollTop: 0, // viewport at the top -> rows 0..4
      viewportHeight: 500,
      overscanPx: 0,
      pinnedRange: { start: 900, end: 901 },
    });
    expect(segments.map((s) => s.kind)).toEqual(["spacer", "rows", "spacer", "rows", "spacer"]);
    const indices = renderedIndices(segments);
    expect(indices).toContain(0);
    expect(indices).toContain(900);
    expect(indices).toContain(901);
    expect(indices).not.toContain(500);
    expect(indices.length).toBeLessThan(20);
  });

  it("folds the phantom regions into the outer spacers and total height", () => {
    const { segments, totalHeight } = computeTranscriptSlices({
      count: 10,
      getHeight: uniform(100),
      scrollTop: 400, // 400 raw - 400 phantomTop = 0 into the loaded rows
      viewportHeight: 300,
      overscanPx: 0,
      phantomTopHeight: 400,
      phantomBottomHeight: 600,
    });
    // 10 rows * 100 + 400 + 600 reserved.
    expect(totalHeight).toBe(2000);
    expect(segmentsHeight(segments, uniform(100))).toBe(2000);
    const first = segments[0];
    const last = segments[segments.length - 1];
    // Leading spacer includes the top phantom; trailing spacer includes the bottom.
    expect(first.kind).toBe("spacer");
    expect((first as { height: number }).height).toBeGreaterThanOrEqual(400);
    expect(last.kind).toBe("spacer");
    expect((last as { height: number }).height).toBeGreaterThanOrEqual(600);
  });

  it("clamps an out-of-range pin instead of producing a bad run", () => {
    const { segments } = computeTranscriptSlices({
      count: 10,
      getHeight: uniform(100),
      scrollTop: 0,
      viewportHeight: 200,
      overscanPx: 0,
      pinnedRange: { start: -5, end: 999 },
    });
    const indices = renderedIndices(segments);
    for (const i of indices) {
      expect(i).toBeGreaterThanOrEqual(0);
      expect(i).toBeLessThan(10);
    }
  });
});
