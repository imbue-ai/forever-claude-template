import { describe, expect, it, vi } from "vitest";

// apiUrl reads a <meta> tag via document, which is absent in the node test
// environment; stub base-path so serve URLs are deterministic.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import {
  attachmentBasename,
  attachmentServeUrl,
  buildMessageWithAttachments,
  cacheAttachmentSize,
  formatFileSize,
  getCachedAttachmentSize,
  isImagePath,
  parseMessageAttachments,
} from "./attachments";

const IMAGE_PATH = "/code/uploads/aaa/diagram.png";
const FILE_PATH = "/code/uploads/bbb/notes.txt";

describe("isImagePath", () => {
  it("recognizes common image extensions", () => {
    expect(isImagePath("a.png")).toBe(true);
    expect(isImagePath("a.JPG")).toBe(true);
    expect(isImagePath("a.jpeg")).toBe(true);
    expect(isImagePath("a.gif")).toBe(true);
    expect(isImagePath("a.webp")).toBe(true);
    expect(isImagePath("a.svg")).toBe(true);
  });

  it("rejects non-image and extensionless names", () => {
    expect(isImagePath("a.pdf")).toBe(false);
    expect(isImagePath("a.txt")).toBe(false);
    expect(isImagePath("README")).toBe(false);
  });
});

describe("attachmentBasename", () => {
  it("returns the final path segment", () => {
    expect(attachmentBasename("/code/uploads/x/diagram.png")).toBe("diagram.png");
    expect(attachmentBasename("lonely")).toBe("lonely");
  });
});

describe("attachmentServeUrl", () => {
  it("maps an absolute upload path to the serve endpoint", () => {
    expect(attachmentServeUrl(IMAGE_PATH)).toBe("/api/uploads/aaa/diagram.png");
  });

  it("percent-encodes each path segment", () => {
    expect(attachmentServeUrl("/x/uploads/id/na me.png")).toBe("/api/uploads/id/na%20me.png");
  });
});

describe("buildMessageWithAttachments", () => {
  it("returns the text unchanged when there are no attachments", () => {
    expect(buildMessageWithAttachments("hello", [])).toBe("hello");
  });

  it("appends a singular line for one attachment", () => {
    expect(buildMessageWithAttachments("look", [IMAGE_PATH])).toBe(`look\n\nSee attachment here: ${IMAGE_PATH}`);
  });

  it("appends a plural, comma-joined line for several attachments", () => {
    expect(buildMessageWithAttachments("look", [IMAGE_PATH, FILE_PATH])).toBe(
      `look\n\nSee attachments here: ${IMAGE_PATH}, ${FILE_PATH}`,
    );
  });

  it("omits the leading text when the message is attachments only", () => {
    expect(buildMessageWithAttachments("", [IMAGE_PATH])).toBe(`See attachment here: ${IMAGE_PATH}`);
  });
});

describe("parseMessageAttachments", () => {
  it("leaves a plain message untouched", () => {
    const parsed = parseMessageAttachments("just a normal message");
    expect(parsed.visibleText).toBe("just a normal message");
    expect(parsed.attachments).toEqual([]);
  });

  it("round-trips the build output back to text and attachment metadata", () => {
    const content = buildMessageWithAttachments("look at these", [IMAGE_PATH, FILE_PATH]);

    const parsed = parseMessageAttachments(content);

    expect(parsed.visibleText).toBe("look at these");
    expect(parsed.attachments).toEqual([
      { path: IMAGE_PATH, name: "diagram.png", isImage: true, url: "/api/uploads/aaa/diagram.png" },
      { path: FILE_PATH, name: "notes.txt", isImage: false, url: "/api/uploads/bbb/notes.txt" },
    ]);
  });

  it("yields empty visible text for an attachments-only message", () => {
    const parsed = parseMessageAttachments(buildMessageWithAttachments("", [IMAGE_PATH]));
    expect(parsed.visibleText).toBe("");
    expect(parsed.attachments.map((a) => a.path)).toEqual([IMAGE_PATH]);
  });

  it("does not strip a similar sentence whose paths are not uploads", () => {
    const content = "See attachment here: /etc/passwd";
    const parsed = parseMessageAttachments(content);
    expect(parsed.visibleText).toBe(content);
    expect(parsed.attachments).toEqual([]);
  });
});

describe("formatFileSize", () => {
  it("formats bytes, kilobytes, and megabytes", () => {
    expect(formatFileSize(512)).toBe("512 B");
    expect(formatFileSize(1024)).toBe("1 KB");
    expect(formatFileSize(1536)).toBe("1.5 KB");
    expect(formatFileSize(1048576)).toBe("1 MB");
  });
});

describe("attachment size cache", () => {
  it("stores and returns a cached size", () => {
    expect(getCachedAttachmentSize("/code/uploads/zzz/cached.bin")).toBeUndefined();
    cacheAttachmentSize("/code/uploads/zzz/cached.bin", 4096);
    expect(getCachedAttachmentSize("/code/uploads/zzz/cached.bin")).toBe(4096);
  });
});
