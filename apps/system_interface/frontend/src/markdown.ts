import m from "mithril";
import DOMPurify from "dompurify";
import { Marked } from "marked";
import { apiUrl } from "./base-path";
import { openImageLightbox } from "./lightbox";

const marked = new Marked({
  breaks: true,
  gfm: true,
});

const TOOL_CALL_PREFIX = "Tool call: ";

export function renderMarkdown(source: string): string {
  const rawHtml = marked.parse(source) as string;
  return DOMPurify.sanitize(rawHtml);
}

// Extensions the backend serves as inline images (mirrors
// file_serving._IMAGE_EXTENSION_TO_MIME_TYPE); anything else is a download
// link, which is not change-checked.
const INLINE_IMAGE_EXTENSION_PATTERN = /\.(png|jpe?g|gif|webp|avif|bmp|ico|svg)$/i;

/** Build the per-message change-checking URL for an absolute on-disk image path. */
export function chatImageUrl(sourcePath: string, eventId: string): string {
  const encodedPath = sourcePath.slice(1).split("/").map(encodeURIComponent).join("/");
  return apiUrl(`/api/chat-images/${encodeURIComponent(eventId)}/${encodedPath}`);
}

// HTTP status the backend returns when a referenced file has changed since its
// message was posted (mirrors file_serving.CHANGED_FILE_STATUS). It is not an
// image, so the <img> load fails; we detect that status and swap in a notice.
const CHAT_FILE_CHANGED_STATUS = 409;
const CHAT_FILE_CHANGED_FALLBACK =
  "This file has been changed. Please revert your workspace or ask your agent to recover it.";

/**
 * Replace a changed image with a plain, non-interactive text notice.
 *
 * A div -- not an img and not a link -- so the notice can never be clicked to
 * enlarge (the lightbox only acts on <img>) or opened/downloaded. This is the
 * whole point of returning a non-image status for a changed file: the "file has
 * been changed" message must not itself masquerade as the file.
 */
function replaceWithChangedNotice(image: HTMLImageElement, message: string): void {
  const notice = document.createElement("div");
  notice.className = "chat-file-changed";
  notice.textContent = message;
  image.replaceWith(notice);
}

/**
 * On an image load error, tell a changed file apart from a broken path.
 *
 * The change-checking route returns the image (200) when unchanged, a non-image
 * CHAT_FILE_CHANGED_STATUS when the file has changed, and 404 for a never-seen
 * missing path; the first two both surface as an <img> error, so re-fetch to
 * read the status. Only a changed file becomes the notice; a genuine 404 (a
 * typo'd path) is left as the browser's broken-image icon.
 */
function handleChatImageLoadError(image: HTMLImageElement): void {
  const url = image.getAttribute("src") ?? "";
  if (!url) return;
  void fetch(url)
    .then(async (response) => {
      if (response.status !== CHAT_FILE_CHANGED_STATUS) return;
      const message = (await response.text()).trim() || CHAT_FILE_CHANGED_FALLBACK;
      replaceWithChangedNotice(image, message);
    })
    .catch(() => {
      // Network error re-checking status: leave the broken-image icon as-is.
    });
}

/**
 * Point every inline chat image at its per-message change-checking URL.
 *
 * Chat markdown references an image by its absolute on-disk path, and the
 * backend serves that path with a one-year immutable cache policy -- so if the
 * file is later overwritten, a new message would show the browser's stale
 * cached copy and an old message would silently change appearance. Routing
 * through /api/chat-images/<event_id>/<path> fixes both: the backend records
 * the file's mtime+size the first time each (event, path) pair is seen and
 * serves the file uncached, so every render refetches; once the file no longer
 * matches, the fetch fails with a changed-file status and the image is replaced
 * by a non-interactive "file has been changed" notice.
 */
function rewriteChatImageSources(container: HTMLElement, eventId: string): void {
  for (const image of Array.from(container.querySelectorAll("img"))) {
    // The raw attribute, not image.src, which the browser resolves to a full URL.
    const src = image.getAttribute("src") ?? "";
    // Only same-origin absolute on-disk paths are change-checked: external URLs
    // ("https://...", "//...") and app routes ("/api/...") pass through.
    if (!src.startsWith("/") || src.startsWith("//") || src.startsWith("/api/")) continue;
    if (!INLINE_IMAGE_EXTENSION_PATTERN.test(src)) continue;
    // Attach the error handler before pointing at the new URL so a changed
    // file's failed load is caught and turned into the notice.
    image.addEventListener("error", () => handleChatImageLoadError(image), { once: true });
    image.setAttribute("src", chatImageUrl(src, eventId));
  }
}

function hasToolCallLine(textContent: string): boolean {
  for (const line of textContent.split("\n")) {
    if (line.trim().startsWith(TOOL_CALL_PREFIX)) {
      return true;
    }
  }
  return false;
}

/*
 * On the backend, we use the --td argument to llm and include
 * the debug output in the stream. For each tool call, the debug
 * output starts with a line that starts with "Tool call: ".
 */
function wrapToolCallBlocks(container: HTMLElement): void {
  for (const preElement of Array.from(container.querySelectorAll("pre"))) {
    if (preElement.parentElement?.classList.contains("tool-call-block")) {
      continue;
    }
    const codeElement = preElement.querySelector("code");
    if (codeElement === null) {
      continue;
    }
    if (!hasToolCallLine(codeElement.textContent ?? "")) {
      continue;
    }

    codeElement.textContent = (codeElement.textContent ?? "").replace(/^\s*\n/, "");

    const wrapper = document.createElement("div");
    wrapper.className = "tool-call-block";
    wrapper.addEventListener("click", () => {
      wrapper.classList.toggle("tool-call-block--expanded");
    });

    preElement.replaceWith(wrapper);
    wrapper.appendChild(preElement);
  }
}

function saveExpandedState(container: HTMLElement): Set<number> {
  const expanded = new Set<number>();
  const blocks = container.querySelectorAll(".tool-call-block");
  blocks.forEach((block, index) => {
    if (block.classList.contains("tool-call-block--expanded")) {
      expanded.add(index);
    }
  });
  return expanded;
}

function restoreExpandedState(container: HTMLElement, expanded: Set<number>): void {
  const blocks = container.querySelectorAll(".tool-call-block");
  blocks.forEach((block, index) => {
    if (expanded.has(index)) {
      block.classList.add("tool-call-block--expanded");
    }
  });
}

function handleMarkdownImageClick(event: MouseEvent): void {
  const target = event.target;
  if (target instanceof HTMLImageElement) {
    event.preventDefault();
    openImageLightbox(target.src, target.alt);
  }
}

export const MarkdownContent: m.Component<{ content: string; eventId?: string }> = {
  oncreate(vnode) {
    const element = vnode.dom as HTMLElement;
    element.innerHTML = renderMarkdown(vnode.attrs.content);
    wrapToolCallBlocks(element);
    if (vnode.attrs.eventId) {
      rewriteChatImageSources(element, vnode.attrs.eventId);
    }
    // Clicking an inline image opens it full-screen. The listener is delegated
    // on the container, which mithril reuses across redraws, so it survives the
    // innerHTML resets in onupdate without needing to be re-attached.
    element.addEventListener("click", handleMarkdownImageClick);
  },
  // Skip the subtree diff (and the onupdate innerHTML rewrite below) whenever the
  // markdown source is unchanged. onupdate re-sets innerHTML, which destroys every
  // text node in the subtree; the browser then collapses any selection anchored in
  // it. Since a global redraw fires on every scroll tick and every streamed event,
  // an unguarded rewrite kills a user's text selection on the first frame. The
  // rendered element is a childless div whose content we manage by hand, so there is
  // nothing for Mithril to diff -- retaining the DOM untouched is always correct when
  // the content matches.
  onbeforeupdate(vnode, old) {
    return vnode.attrs.content !== old.attrs.content;
  },
  onupdate(vnode) {
    const element = vnode.dom as HTMLElement;
    const expanded = saveExpandedState(element);
    element.innerHTML = renderMarkdown(vnode.attrs.content);
    wrapToolCallBlocks(element);
    if (vnode.attrs.eventId) {
      rewriteChatImageSources(element, vnode.attrs.eventId);
    }
    restoreExpandedState(element, expanded);
  },
  view() {
    return m("div", { class: "message-content markdown-content" });
  },
};
