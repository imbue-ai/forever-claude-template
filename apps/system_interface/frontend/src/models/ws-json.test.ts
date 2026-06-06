import { afterEach, describe, expect, it, vi } from "vitest";
import { parseJsonMessage } from "./ws-json";

interface SampleMessage {
  type: string;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("parseJsonMessage", () => {
  it("parses a well-formed payload", () => {
    const result = parseJsonMessage<SampleMessage>('{"type":"agents_updated"}');
    expect(result).toEqual({ type: "agents_updated" });
  });

  it("returns null on a malformed payload without throwing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(() => parseJsonMessage<SampleMessage>("{not json")).not.toThrow();
    expect(parseJsonMessage<SampleMessage>("{not json")).toBeNull();
    expect(warn).toHaveBeenCalled();
  });
});
