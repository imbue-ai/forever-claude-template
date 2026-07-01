/**
 * A full-screen image viewer ("lightbox") opened by clicking an inline chat
 * image. It shows the image enlarged over a dimmed backdrop with a header:
 * the image's title (alt text, or the filename) centered, and Download and
 * close (X) icon buttons at the top right. It closes on a backdrop click, the X
 * button, or the Escape key.
 *
 * This is imperative (plain DOM) rather than a mithril component because chat
 * messages are rendered via `innerHTML` (see markdown.ts), so the images it
 * applies to are not part of mithril's vnode tree.
 */

// Inline feather-style icons (static markup, safe to assign via innerHTML).
const DOWNLOAD_ICON_SVG =
  '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"' +
  ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>' +
  '<polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';

const CLOSE_ICON_SVG =
  '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"' +
  ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

function filenameFromUrl(imageUrl: string): string {
  try {
    const path = new URL(imageUrl, window.location.href).pathname;
    const basename = path.split("/").pop() ?? "";
    return decodeURIComponent(basename) || "image";
  } catch {
    return "image";
  }
}

let activeOverlay: HTMLElement | null = null;

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    closeImageLightbox();
  }
}

// Close when a backdrop element (the overlay padding or the empty area around
// the image) is clicked directly -- not when a child (image, header, button) is.
function onBackdropClick(event: MouseEvent): void {
  if (event.target === event.currentTarget) {
    closeImageLightbox();
  }
}

export function closeImageLightbox(): void {
  if (activeOverlay === null) {
    return;
  }
  activeOverlay.remove();
  activeOverlay = null;
  document.removeEventListener("keydown", onKeydown);
}

export function openImageLightbox(imageUrl: string, altText: string): void {
  // Only one lightbox open at a time.
  closeImageLightbox();

  const filename = filenameFromUrl(imageUrl);

  const overlay = document.createElement("div");
  overlay.className = "image-lightbox-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.addEventListener("click", onBackdropClick);

  const header = document.createElement("div");
  header.className = "image-lightbox-header";

  const title = document.createElement("div");
  title.className = "image-lightbox-title";
  title.textContent = altText || filename;

  const actions = document.createElement("div");
  actions.className = "image-lightbox-actions";

  const downloadLink = document.createElement("a");
  downloadLink.className = "image-lightbox-iconbtn";
  downloadLink.href = imageUrl;
  downloadLink.download = filename;
  // Same-origin images download in place; a cross-origin (public-URL) image the
  // browser refuses to download opens in a new tab rather than navigating away.
  downloadLink.target = "_blank";
  downloadLink.rel = "noopener noreferrer";
  downloadLink.title = "Download";
  downloadLink.setAttribute("aria-label", "Download image");
  downloadLink.innerHTML = DOWNLOAD_ICON_SVG;

  const closeButton = document.createElement("button");
  closeButton.className = "image-lightbox-iconbtn";
  closeButton.type = "button";
  closeButton.title = "Close";
  closeButton.setAttribute("aria-label", "Close image viewer");
  closeButton.innerHTML = CLOSE_ICON_SVG;
  closeButton.addEventListener("click", closeImageLightbox);

  actions.appendChild(downloadLink);
  actions.appendChild(closeButton);
  header.appendChild(title);
  header.appendChild(actions);

  const body = document.createElement("div");
  body.className = "image-lightbox-body";
  body.addEventListener("click", onBackdropClick);

  const image = document.createElement("img");
  image.className = "image-lightbox-img";
  image.src = imageUrl;
  image.alt = altText;
  body.appendChild(image);

  overlay.appendChild(header);
  overlay.appendChild(body);

  document.body.appendChild(overlay);
  activeOverlay = overlay;
  document.addEventListener("keydown", onKeydown);
}
