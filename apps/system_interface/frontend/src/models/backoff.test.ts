import { describe, expect, it } from "vitest";
import { computeBackoffDelay, ReconnectBackoff, RECONNECT_BASE_MS, RECONNECT_CAP_MS } from "./backoff";

describe("computeBackoffDelay", () => {
  it("grows exponentially and caps, with no jitter at random=0.5", () => {
    const noJitter = () => 0.5;
    expect(computeBackoffDelay(0, noJitter)).toBe(RECONNECT_BASE_MS);
    expect(computeBackoffDelay(1, noJitter)).toBe(RECONNECT_BASE_MS * 2);
    expect(computeBackoffDelay(2, noJitter)).toBe(RECONNECT_BASE_MS * 4);
    expect(computeBackoffDelay(3, noJitter)).toBe(RECONNECT_BASE_MS * 8);
    // Far out, the exponential is clamped to the cap.
    expect(computeBackoffDelay(20, noJitter)).toBe(RECONNECT_CAP_MS);
  });

  it("keeps delays within the +/-20% jitter band at the extremes", () => {
    const capped = RECONNECT_BASE_MS * 4;
    // random=0 -> jitter at -20%; random just under 1 -> jitter near +20%.
    expect(computeBackoffDelay(2, () => 0)).toBe(Math.round(capped * 0.8));
    expect(computeBackoffDelay(2, () => 1)).toBe(Math.round(capped * 1.2));
  });
});

describe("ReconnectBackoff", () => {
  it("advances on each nextDelay and resets to base", () => {
    const backoff = new ReconnectBackoff(() => 0.5);
    expect(backoff.nextDelay()).toBe(RECONNECT_BASE_MS);
    expect(backoff.nextDelay()).toBe(RECONNECT_BASE_MS * 2);
    expect(backoff.nextDelay()).toBe(RECONNECT_BASE_MS * 4);
    backoff.reset();
    expect(backoff.nextDelay()).toBe(RECONNECT_BASE_MS);
  });
});
