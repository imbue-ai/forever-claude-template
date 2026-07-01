/**
 * Chat file attachments: the wire format shared between the composer and the
 * transcript renderer, plus the upload/serve/delete client.
 *
 * The composer appends a human-readable line naming each uploaded file by its
 * absolute path on the agent VM (e.g. "See attachment here: /code/uploads/<id>/
 * <name>") to the message it sends. Because the agent records that
 * text in its session transcript, the block is persisted for free: on render we
 * strip it back off (so the raw path never shows in the bubble) and rebuild the
 * image thumbnails / file chips from the parsed paths, whose preview URLs serve
 * the VM copy. Appending in the frontend (rather than server-side) keeps the
 * optimistic pending bubble's content identical to what the agent later records,
 * so the existing content-match reconciliation keeps working.
 */

import { apiUrl } from "../base-path";

/** Marker segment present in every stored-attachment path. Used to detect the
 *  appended attachment block in persisted message content and to derive a
 *  preview URL from an absolute path. */
export const UPLOADS_PATH_MARKER = "/uploads/";

/** A file the user uploaded to the agent VM and attached to the current draft. */
export interface UploadedAttachment {
  /** Absolute path on the agent VM, referenced in the message sent to the agent. */
  path: string;
  /** Display filename (the basename of the path). */
  name: string;
  /** Size in bytes. */
  size: number;
  /** Whether to render an image thumbnail (vs a file chip). */
  isImage: boolean;
  /** Same-origin URL that serves the stored file for inline previews. */
  url: string;
}

/** An attachment parsed back out of a sent message's content, for rendering. */
export interface MessageAttachment {
  path: string;
  name: string;
  isImage: boolean;
  url: string;
}

const SINGLE_ATTACHMENT_PREFIX = "See attachment here: ";
const MULTIPLE_ATTACHMENT_PREFIX = "See attachments here: ";

const IMAGE_EXTENSION_RE = /\.(png|jpe?g|gif|webp|bmp|avif|svg|ico|tiff?|heic|heif)$/i;

const ATTACHMENT_BLOCK_RE = /(?:^|\n\n)See attachments? here: ([^\n]+)\s*$/;

export function isImagePath(path: string): boolean {
  return IMAGE_EXTENSION_RE.test(path);
}

export function attachmentBasename(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}

export function attachmentServeUrl(path: string): string {
  const markerIndex = path.indexOf(UPLOADS_PATH_MARKER);
  const relativePath = markerIndex >= 0 ? path.slice(markerIndex + UPLOADS_PATH_MARKER.length) : path;
  const encodedRelativePath = relativePath
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return apiUrl(`/api/uploads/${encodedRelativePath}`);
}

/**
 * Build the message text delivered to the agent: the user's text followed by a
 * human-readable line naming each attachment by path. Returns the text unchanged
 * when there are no attachments.
 */
export function buildMessageWithAttachments(text: string, attachmentPaths: readonly string[]): string {
  if (attachmentPaths.length === 0) {
    return text;
  }
  const prefix = attachmentPaths.length === 1 ? SINGLE_ATTACHMENT_PREFIX : MULTIPLE_ATTACHMENT_PREFIX;
  const block = prefix + attachmentPaths.join(", ");
  return text.length > 0 ? `${text}\n\n${block}` : block;
}

/**
 * Split a sent message's content into the user-visible text and the attachment
 * block appended by ``buildMessageWithAttachments``. The block is only stripped
 * when every listed path is an uploads path, so a user who merely types a
 * similar sentence is never misread.
 */
export function parseMessageAttachments(content: string): { visibleText: string; attachments: MessageAttachment[] } {
  const match = content.match(ATTACHMENT_BLOCK_RE);
  if (match === null) {
    return { visibleText: content, attachments: [] };
  }
  const paths = match[1].split(", ").map((path) => path.trim());
  if (!paths.every((path) => path.includes(UPLOADS_PATH_MARKER))) {
    return { visibleText: content, attachments: [] };
  }
  const visibleText = content.slice(0, match.index ?? 0).replace(/\s+$/, "");
  const attachments = paths.map((path) => ({
    path,
    name: attachmentBasename(path),
    isImage: isImagePath(path),
    url: attachmentServeUrl(path),
  }));
  return { visibleText, attachments };
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ["KB", "MB", "GB", "TB"];
  let size = bytes / 1024;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size = size / 1024;
    unitIndex = unitIndex + 1;
  }
  const rounded = size >= 10 ? Math.round(size) : Math.round(size * 10) / 10;
  return `${rounded} ${units[unitIndex]}`;
}

// --- Stored-size cache + lazy HEAD lookup ----------------------------------
//
// The size of a freshly-uploaded file is known from the upload response and
// cached here. After a page reload the cache is empty, so a file chip reads the
// size from a one-off HEAD request to the serve endpoint (images show a
// thumbnail instead and never need this).

const _sizeByPath = new Map<string, number>();

export function cacheAttachmentSize(path: string, size: number): void {
  _sizeByPath.set(path, size);
}

export function getCachedAttachmentSize(path: string): number | undefined {
  return _sizeByPath.get(path);
}

export async function fetchAttachmentSize(path: string): Promise<number | undefined> {
  const cached = _sizeByPath.get(path);
  if (cached !== undefined) {
    return cached;
  }
  try {
    const response = await fetch(attachmentServeUrl(path), { method: "HEAD" });
    const lengthHeader = response.headers.get("Content-Length");
    if (lengthHeader !== null) {
      const size = Number.parseInt(lengthHeader, 10);
      if (Number.isFinite(size)) {
        _sizeByPath.set(path, size);
        return size;
      }
    }
  } catch (_error) {
    // Network/HEAD failure: the chip simply renders without a size.
    return undefined;
  }
  return undefined;
}

// --- Upload client ----------------------------------------------------------

interface AttachmentUploadResponseBody {
  path: string;
  size: number;
}

function makeUploadedAttachment(path: string, size: number): UploadedAttachment {
  cacheAttachmentSize(path, size);
  return {
    path,
    name: attachmentBasename(path),
    size,
    isImage: isImagePath(path),
    url: attachmentServeUrl(path),
  };
}

/** Upload a single file to the agent VM, returning its stored metadata. Throws
 *  on a non-2xx response. */
export async function uploadAttachment(file: File): Promise<UploadedAttachment> {
  const formData = new FormData();
  formData.append("file", file, file.name);
  const response = await fetch(apiUrl("/api/uploads"), { method: "POST", body: formData });
  if (!response.ok) {
    throw new Error(`Upload failed (HTTP ${response.status})`);
  }
  const body = (await response.json()) as AttachmentUploadResponseBody;
  return makeUploadedAttachment(body.path, body.size);
}

/** Delete a previously-uploaded attachment from the agent VM. Best-effort: a
 *  failed request is swallowed since the caller is removing it from the UI
 *  regardless. */
export async function deleteAttachment(path: string): Promise<void> {
  try {
    await fetch(attachmentServeUrl(path), { method: "DELETE" });
  } catch (_error) {
    // Best-effort cleanup; nothing to recover if the delete request fails.
    return;
  }
}
