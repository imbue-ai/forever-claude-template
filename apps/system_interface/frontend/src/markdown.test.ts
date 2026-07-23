import { describe, expect, it, vi } from "vitest";

// apiUrl reads a <meta> tag via document, absent in the node test environment.
vi.mock("./base-path", () => ({ apiUrl: (path: string) => path }));
// lightbox touches the DOM imperatively; markdown.ts only needs its export to exist.
vi.mock("./lightbox", () => ({ openImageLightbox: vi.fn() }));

import { chatFileUrl } from "./markdown";

describe("chatFileUrl", () => {
  it("routes an absolute on-disk path through the per-message change-checking endpoint", () => {
    expect(chatFileUrl("/mngr/code/runtime/chat-images/chart.png", "event-1")).toBe(
      "/api/chat-files/event-1/mngr/code/runtime/chat-images/chart.png",
    );
  });

  it("works for non-image download paths too", () => {
    expect(chatFileUrl("/mngr/code/runtime/chat-files/report.pdf", "event-1")).toBe(
      "/api/chat-files/event-1/mngr/code/runtime/chat-files/report.pdf",
    );
  });

  it("percent-encodes path segments and the event id", () => {
    expect(chatFileUrl("/tmp/my report.pdf", "id/with?chars")).toBe(
      "/api/chat-files/id%2Fwith%3Fchars/tmp/my%20report.pdf",
    );
  });
});
