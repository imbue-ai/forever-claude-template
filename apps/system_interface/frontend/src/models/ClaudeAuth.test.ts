import { beforeEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request/redraw via hoisted mocks (same pattern as
// StreamingMessage.test.ts) so the test controls the status probe's reply.
const { mockRequest, mockRedraw } = vi.hoisted(() => ({
  mockRequest: vi.fn(),
  mockRedraw: vi.fn(),
}));
vi.mock("mithril", () => ({
  default: { request: mockRequest, redraw: mockRedraw },
}));
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

// The module holds page-load-scoped state (modal flag + one-shot probe
// guard), so each test imports a fresh copy.
async function freshClaudeAuth(): Promise<typeof import("./ClaudeAuth")> {
  vi.resetModules();
  return import("./ClaudeAuth");
}

describe("openLoginModalForEmptyTranscript", () => {
  beforeEach(() => {
    mockRequest.mockReset();
    mockRedraw.mockReset();
  });

  it("opens the login modal when the auth probe reports signed-out", async () => {
    const auth = await freshClaudeAuth();
    mockRequest.mockResolvedValue({ logged_in: false });
    auth.openLoginModalForEmptyTranscript();
    await vi.waitFor(() => expect(auth.isLoginModalOpen()).toBe(true));
    expect(mockRequest).toHaveBeenCalledWith({ method: "GET", url: "/api/claude-auth/status" });
  });

  it("does not open the modal when authenticated", async () => {
    const auth = await freshClaudeAuth();
    mockRequest.mockResolvedValue({ logged_in: true });
    auth.openLoginModalForEmptyTranscript();
    // Let the resolved promise chain flush.
    await Promise.resolve();
    await Promise.resolve();
    expect(auth.isLoginModalOpen()).toBe(false);
  });

  it("probes at most once per page load, so a dismissed modal is not reopened by other empty panels", async () => {
    const auth = await freshClaudeAuth();
    mockRequest.mockResolvedValue({ logged_in: false });
    auth.openLoginModalForEmptyTranscript();
    await vi.waitFor(() => expect(auth.isLoginModalOpen()).toBe(true));
    auth.closeLoginModal();
    auth.openLoginModalForEmptyTranscript();
    await Promise.resolve();
    await Promise.resolve();
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(auth.isLoginModalOpen()).toBe(false);
  });

  it("swallows probe failures (the reactive is_auth_error path still stands)", async () => {
    const auth = await freshClaudeAuth();
    mockRequest.mockRejectedValue(new Error("status endpoint unavailable"));
    auth.openLoginModalForEmptyTranscript();
    await Promise.resolve();
    await Promise.resolve();
    expect(auth.isLoginModalOpen()).toBe(false);
  });
});
