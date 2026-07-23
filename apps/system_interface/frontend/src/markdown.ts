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

// App-route prefixes that share the absolute-path shape but are not on-disk
// files, so their URLs must not be change-checked (mirrors the backend guard in
// chat_file_timestamps._RESERVED_URL_PREFIXES).
const RESERVED_URL_PREFIXES = ["/api/", "/assets/", "/plugins/", "/service/", "/_"];

/** Whether an attribute value is an absolute on-disk path we should change-check. */
function isChangeCheckablePath(value: string): boolean {
  if (!value.startsWith("/") || value.startsWith("//")) return false;
  return !RESERVED_URL_PREFIXES.some((prefix) => value.startsWith(prefix));
}

/** Build the per-message change-checking URL for an absolute on-disk file path. */
export function chatFileUrl(sourcePath: string, eventId: string): string {
  const encodedPath = sourcePath.slice(1).split("/").map(encodeURIComponent).join("/");
  return apiUrl(`/api/chat-files/${encodeURIComponent(eventId)}/${encodedPath}`);
}

// HTTP status the backend returns when a referenced file has changed since its
// message was posted (mirrors file_serving.CHANGED_FILE_STATUS). It is not the
// file, so an image's <img> load fails and a link's download would be wrong; we
// detect that status and swap in a stale notice instead.
const CHAT_FILE_CHANGED_STATUS = 409;
const CHAT_FILE_CHANGED_FALLBACK =
  "This file has been changed. Please revert your workspace or ask your agent to recover it.";

/**
 * Replace a changed image or link with a plain, non-interactive text notice.
 *
 * A div -- not an img and not a link -- so the notice can never be clicked to
 * enlarge, open, or download. This is the whole point of the change check: once
 * a file is stale, the message must not keep offering it as if it were still
 * the version that was posted.
 */
function replaceWithChangedNotice(element: Element, message: string): void {
  const notice = document.createElement("div");
  notice.className = "chat-file-changed";
  notice.textContent = message;
  element.replaceWith(notice);
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
 * Proactively replace a changed download link with the stale notice.
 *
 * A link has no auto-load to fail on, so -- unlike an image -- we check its
 * status on render (a HEAD, which the endpoint answers from a cheap stat) and
 * swap in the notice when the file has changed. A 200 (unchanged) leaves the
 * link as a normal download; a 404 (typo'd path) leaves it alone as a dead
 * link.
 */
function checkChatLinkFreshness(anchor: HTMLAnchorElement, url: string): void {
  void fetch(url, { method: "HEAD" })
    .then((response) => {
      if (response.status === CHAT_FILE_CHANGED_STATUS) {
        replaceWithChangedNotice(anchor, CHAT_FILE_CHANGED_FALLBACK);
      }
    })
    .catch(() => {
      // Network error checking status: leave the link as-is.
    });
}

/**
 * Point every chat-referenced file -- an inline image's src or a download
 * link's href -- at its per-message change-checking URL.
 *
 * Chat markdown references a file by its absolute on-disk path. The backend
 * records the file's mtime+size the first time each (event, path) pair is seen
 * and serves it uncached, so every render re-checks. Once the file no longer
 * matches, the backend reports it changed: an image's load fails and is
 * replaced by a non-interactive stale notice; a link's render-time check
 * replaces it with the same notice, so a stale file is never silently shown or
 * downloaded.
 */
function rewriteChatFileSources(container: HTMLElement, eventId: string): void {
  for (const image of Array.from(container.querySelectorAll("img"))) {
    // The raw attribute, not image.src, which the browser resolves to a full URL.
    const src = image.getAttribute("src") ?? "";
    if (!isChangeCheckablePath(src)) continue;
    // Attach the error handler before pointing at the new URL so a changed
    // file's failed load is caught and turned into the notice.
    image.addEventListener("error", () => handleChatImageLoadError(image), { once: true });
    image.setAttribute("src", chatFileUrl(src, eventId));
  }
  for (const anchor of Array.from(container.querySelectorAll("a"))) {
    const href = anchor.getAttribute("href") ?? "";
    if (!isChangeCheckablePath(href)) continue;
    const url = chatFileUrl(href, eventId);
    anchor.setAttribute("href", url);
    checkChatLinkFreshness(anchor, url);
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
      rewriteChatFileSources(element, vnode.attrs.eventId);
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
      rewriteChatFileSources(element, vnode.attrs.eventId);
    }
    restoreExpandedState(element, expanded);
  },
  view() {
    return m("div", { class: "message-content markdown-content" });
  },
};
