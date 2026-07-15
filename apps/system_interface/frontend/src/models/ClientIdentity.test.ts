import { describe, expect, it } from "vitest";
import { classifyDeviceKind } from "./ClientIdentity";

describe("classifyDeviceKind", () => {
  it("trusts userAgentData.mobile when present", () => {
    expect(classifyDeviceKind(true, "Mozilla/5.0 (X11; Linux x86_64)")).toBe("mobile");
    expect(classifyDeviceKind(false, "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)")).toBe("desktop");
  });

  it("falls back to UA-string sniffing when userAgentData is absent", () => {
    expect(classifyDeviceKind(undefined, "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148")).toBe(
      "mobile",
    );
    expect(classifyDeviceKind(undefined, "Mozilla/5.0 (Linux; Android 14; Pixel 8)")).toBe("mobile");
    expect(classifyDeviceKind(undefined, "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")).toBe("desktop");
    expect(classifyDeviceKind(undefined, "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")).toBe("desktop");
  });
});
