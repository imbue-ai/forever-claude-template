import { describe, expect, it } from "vitest";
import { describeRequestError, isBackendUnreachableError } from "./request-error";

describe("describeRequestError", () => {
  it("prefers response.detail over message and code", () => {
    const error = { response: { detail: "agent not found" }, message: "Not Found", code: 404 };
    expect(describeRequestError(error)).toBe("agent not found");
  });

  it("returns a real non-empty message when there is no detail", () => {
    const error = { message: "Bad Request", code: 400 };
    expect(describeRequestError(error)).toBe("Bad Request");
  });

  it('rejects a literal "null" message and falls through to the HTTP-code fallback', () => {
    const error = { message: "null", code: 503 };
    expect(describeRequestError(error)).toBe("request failed (HTTP 503)");
  });

  it('rejects a literal "undefined" message and falls through to the HTTP-code fallback', () => {
    const error = { message: "undefined", code: 502 };
    expect(describeRequestError(error)).toBe("request failed (HTTP 502)");
  });

  it('rejects a "null" message and returns unknown error when there is no usable code', () => {
    const error = { message: "null", code: 0 };
    expect(describeRequestError(error)).toBe("unknown error");
  });

  it("returns unknown error for null and undefined", () => {
    expect(describeRequestError(null)).toBe("unknown error");
    expect(describeRequestError(undefined)).toBe("unknown error");
  });
});

describe("isBackendUnreachableError", () => {
  it("returns true for network-failure and proxy gateway codes", () => {
    expect(isBackendUnreachableError({ code: 0 })).toBe(true);
    expect(isBackendUnreachableError({ code: 502 })).toBe(true);
    expect(isBackendUnreachableError({ code: 503 })).toBe(true);
    expect(isBackendUnreachableError({ code: 504 })).toBe(true);
  });

  it("returns true for the stringified-nullish message artifact", () => {
    expect(isBackendUnreachableError({ message: "null" })).toBe(true);
    expect(isBackendUnreachableError({ message: "undefined" })).toBe(true);
  });

  it("returns false for a normal application error with a real JSON detail", () => {
    const error = { response: { detail: "agent not found" }, message: "Not Found", code: 404 };
    expect(isBackendUnreachableError(error)).toBe(false);
  });

  it("returns false for null, undefined, and string errors", () => {
    expect(isBackendUnreachableError(null)).toBe(false);
    expect(isBackendUnreachableError(undefined)).toBe(false);
    expect(isBackendUnreachableError("some error")).toBe(false);
  });
});
