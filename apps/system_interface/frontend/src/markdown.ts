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
// files, so their URLs must not be rewritten (mirrors the backend's guard in
// chat_file_snapshots._RESERVED_URL_PREFIXES).
const RESERVED_URL_PREFIXES = ["/api/", "/assets/", "/plugins/", "/service/", "/_"];

/** Whether an attribute value is an absolute on-disk path we should snapshot. */
function isSnapshottablePath(value: string): boolean {
  if (!value.startsWith("/") || value.startsWith("//")) return false;
  return !RESERVED_URL_PREFIXES.some((prefix) => value.startsWith(prefix));
}

/** Build the per-message snapshot URL for an absolute on-disk file path. */
export function chatFileSnapshotUrl(sourcePath: string, eventId: string): string {
  const encodedPath = sourcePath.slice(1).split("/").map(encodeURIComponent).join("/");
  return apiUrl(`/api/chat-files/${encodeURIComponent(eventId)}/${encodedPath}`);
}

/**
 * Point every chat-referenced file -- an inline image's src or a download
 * link's href -- at its per-message snapshot.
 *
 * Chat markdown references a file by its absolute on-disk path. Images are
 * served with a one-year immutable cache, so an overwrite would leave a new
 * message showing the browser's stale copy and an old message showing the new
 * content; downloads revalidate, but clicking a link in an old message still
 * hands over whatever is on disk now. Routing through
 * /api/chat-files/<event_id>/<path> fixes both: the backend freezes the file's
 * bytes the first time each (event, path) pair is seen, the event id makes the
 * URL fresh for every new message, and an old message's URL resolves to its
 * frozen copy forever.
 */
function rewriteChatFileSources(container: HTMLElement, eventId: string): void {
  for (const image of Array.from(container.querySelectorAll("img"))) {
    // The raw attribute, not image.src, which the browser resolves to a full URL.
    const src = image.getAttribute("src") ?? "";
    if (!isSnapshottablePath(src)) continue;
    image.setAttribute("src", chatFileSnapshotUrl(src, eventId));
  }
  for (const anchor of Array.from(container.querySelectorAll("a"))) {
    const href = anchor.getAttribute("href") ?? "";
    if (!isSnapshottablePath(href)) continue;
    anchor.setAttribute("href", chatFileSnapshotUrl(href, eventId));
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
