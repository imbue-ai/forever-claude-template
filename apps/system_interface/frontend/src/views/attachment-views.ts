/**
 * Renders the attachments of a sent message: image thumbnails for images and
 * filename/size chips for everything else. Shared by the user-message bubble in
 * the main transcript and by the optimistic pending bubble (both go through
 * ``renderUserMessage``).
 */

import m from "mithril";
import { fetchAttachmentSize } from "../models/attachments";
import { formatFileSize } from "../models/attachments";
import { getCachedAttachmentSize } from "../models/attachments";
import type { MessageAttachment } from "../models/attachments";

const FILE_ICON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M14 3v4a1 1 0 0 0 1 1h4"/>' +
  '<path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z"/></svg>';

/**
 * A non-image attachment chip. Renders the filename and (when known) the size;
 * the size is filled in imperatively after a HEAD request so it appears even
 * when the bubble subtree is memoized and not re-diffed on redraw.
 */
function AttachmentFileChip(): m.Component<{ attachment: MessageAttachment }> {
  return {
    view(vnode) {
      const attachment = vnode.attrs.attachment;
      const knownSize = getCachedAttachmentSize(attachment.path);
      return m(
        "a",
        {
          class: "message-attachment-file",
          href: attachment.url,
          target: "_blank",
          rel: "noopener",
          title: attachment.name,
        },
        [
          m("span", { class: "message-attachment-file-icon" }, m.trust(FILE_ICON_SVG)),
          m("span", { class: "message-attachment-file-meta" }, [
            m("span", { class: "message-attachment-file-name" }, attachment.name),
            m(
              "span",
              { class: "message-attachment-file-size" },
              knownSize !== undefined ? formatFileSize(knownSize) : "",
            ),
          ]),
        ],
      );
    },
    oncreate(vnode) {
      const attachment = vnode.attrs.attachment;
      if (getCachedAttachmentSize(attachment.path) !== undefined) {
        return;
      }
      const chipElement = vnode.dom as HTMLElement;
      void fetchAttachmentSize(attachment.path).then((size) => {
        if (size === undefined) {
          return;
        }
        const sizeElement = chipElement.querySelector(".message-attachment-file-size");
        if (sizeElement !== null) {
          sizeElement.textContent = formatFileSize(size);
        }
      });
    },
  };
}

export function renderMessageAttachments(attachments: MessageAttachment[]): m.Vnode | null {
  if (attachments.length === 0) {
    return null;
  }
  const images = attachments.filter((attachment) => attachment.isImage);
  const files = attachments.filter((attachment) => !attachment.isImage);

  const sections: m.Children[] = [];
  if (images.length > 0) {
    sections.push(
      m(
        "div",
        { class: "message-attachment-images" },
        images.map((attachment) =>
          m("img", {
            key: attachment.path,
            class: "message-attachment-image",
            src: attachment.url,
            alt: attachment.name,
            loading: "lazy",
          }),
        ),
      ),
    );
  }
  if (files.length > 0) {
    sections.push(
      m(
        "div",
        { class: "message-attachment-files" },
        files.map((attachment) => m(AttachmentFileChip, { key: attachment.path, attachment })),
      ),
    );
  }
  return m("div", { class: "message-attachments" }, sections);
}
