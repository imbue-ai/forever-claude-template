import { describe, expect, it, vi } from "vitest";

// apiUrl reads a <meta> tag via document, which is absent in the node test
// environment; stub base-path so serve URLs are deterministic.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import {
  attachmentBasename,
  attachmentMarkdown,
  attachmentServeUrl,
  buildMessageWithAttachments,
  formatFileSize,
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
    expect(isImagePath("a.avif")).toBe(true);
    expect(isImagePath("a.svg")).toBe(true);
  });

  it("rejects non-image and extensionless names", () => {
    expect(isImagePath("a.pdf")).toBe(false);
    expect(isImagePath("a.txt")).toBe(false);
    expect(isImagePath("README")).toBe(false);
  });

  it("rejects image formats browsers cannot decode inline", () => {
    // These render as a broken <img>, so they must fall through to a download link.
    expect(isImagePath("photo.heic")).toBe(false);
    expect(isImagePath("photo.heif")).toBe(false);
    expect(isImagePath("scan.tiff")).toBe(false);
    expect(isImagePath("scan.tif")).toBe(false);
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

describe("attachmentMarkdown", () => {
  it("renders an image as inline image markdown, path in both alt text and URL", () => {
    expect(attachmentMarkdown(IMAGE_PATH)).toBe(`![${IMAGE_PATH}](${IMAGE_PATH})`);
  });

  it("renders a non-image as a plain download link", () => {
    expect(attachmentMarkdown(FILE_PATH)).toBe(`[${FILE_PATH}](${FILE_PATH})`);
  });
});

describe("buildMessageWithAttachments", () => {
  it("returns the text unchanged when there are no attachments", () => {
    expect(buildMessageWithAttachments("hello", [])).toBe("hello");
  });

  it("appends a singular line whose value is the attachment markdown", () => {
    expect(buildMessageWithAttachments("look", [IMAGE_PATH])).toBe(
      `look\n\nSee attachment here: ![${IMAGE_PATH}](${IMAGE_PATH})`,
    );
  });

  it("appends a plural block with one attachment per line, mixing images and download links", () => {
    expect(buildMessageWithAttachments("look", [IMAGE_PATH, FILE_PATH])).toBe(
      `look\n\nSee attachments here: ![${IMAGE_PATH}](${IMAGE_PATH})\n[${FILE_PATH}](${FILE_PATH})`,
    );
  });

  it("omits the leading text when the message is attachments only", () => {
    expect(buildMessageWithAttachments("", [IMAGE_PATH])).toBe(`See attachment here: ![${IMAGE_PATH}](${IMAGE_PATH})`);
  });
});

describe("parseMessageAttachments", () => {
  it("leaves a plain message untouched", () => {
    const parsed = parseMessageAttachments("just a normal message");
    expect(parsed.visibleText).toBe("just a normal message");
    expect(parsed.attachmentBlock).toBeNull();
  });

  it("splits the build output into visible text and the verbatim markdown block", () => {
    const content = buildMessageWithAttachments("look at these", [IMAGE_PATH, FILE_PATH]);

    const parsed = parseMessageAttachments(content);

    expect(parsed.visibleText).toBe("look at these");
    expect(parsed.attachmentBlock).toBe(
      `See attachments here: ![${IMAGE_PATH}](${IMAGE_PATH})\n[${FILE_PATH}](${FILE_PATH})`,
    );
  });

  it("yields empty visible text for an attachments-only message", () => {
    const parsed = parseMessageAttachments(buildMessageWithAttachments("", [IMAGE_PATH]));
    expect(parsed.visibleText).toBe("");
    expect(parsed.attachmentBlock).toBe(`See attachment here: ![${IMAGE_PATH}](${IMAGE_PATH})`);
  });

  it("does not treat a similar sentence whose paths are not uploads as a block", () => {
    const content = "See attachment here: /etc/passwd";
    const parsed = parseMessageAttachments(content);
    expect(parsed.visibleText).toBe(content);
    expect(parsed.attachmentBlock).toBeNull();
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
