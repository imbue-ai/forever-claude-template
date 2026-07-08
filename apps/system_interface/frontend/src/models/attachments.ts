/**
 * Chat file attachments: the wire format shared between the composer and the
 * transcript renderer, plus the upload/serve/delete client.
 *
 * The composer appends a "See attachment here:" line to the message it sends,
 * whose comma-separated values are the markdown for each uploaded file: an
 * inline image (``![path](path)``) for images, a download link (``[path](path)``)
 * otherwise, each referencing the file by its absolute path on the agent VM
 * (e.g. "/code/uploads/<id>/<name>"). The line stays visible in the bubble and
 * renders through the shared markdown renderer -- the system interface serves
 * the file at that absolute path -- so the attachment is transparent to both the
 * reader and the agent (which records the same text in its transcript, and can
 * open the file at the path it names). Appending in the frontend (rather than
 * server-side) keeps the optimistic pending bubble's content identical to what
 * the agent later records, so the existing content-match reconciliation works.
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
  /** Whether the composer preview shows an image thumbnail (vs a file icon). */
  isImage: boolean;
  /** Same-origin URL that serves the stored file for inline previews. */
  url: string;
}

const SINGLE_ATTACHMENT_PREFIX = "See attachment here: ";
const MULTIPLE_ATTACHMENT_PREFIX = "See attachments here: ";

// Only formats browsers can decode inline: an image path becomes an inline
// markdown image (``![]``), so a format the browser can't display (tiff,
// heic/heif) would render a broken image -- those fall through to a download
// link (``[]``) instead. Matches the inline formats the file server serves.
const IMAGE_EXTENSION_RE = /\.(png|jpe?g|gif|webp|avif|bmp|svg|ico)$/i;

// Group 1 captures the whole "See attachment here: ..." block (rendered as
// markdown by the bubble); group 2 captures just the newline-separated values,
// used to verify the block is genuinely ours before treating it as one. The
// user text is set off by a blank line (\n\n), while the items within the block
// are single-newline separated -- so ([\s\S]+?) may span lines to the end.
const ATTACHMENT_BLOCK_RE = /(?:^|\n\n)(See attachments? here: ([\s\S]+?))\s*$/;

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
 * Markdown for one attachment, referencing the file by its absolute on-disk
 * path. An image renders inline (``![path](path)``); any other file becomes a
 * download link (``[path](path)``). The absolute path is used as both the label
 * / alt text and the URL, so the system interface serves the file at that path
 * and the path stays visible to the reader.
 *
 * No escaping is needed: the backend stores each upload under a hex uuid dir
 * with a ``secure_filename``-sanitized basename, so a stored path only contains
 * ``[A-Za-z0-9._-]`` segments -- no spaces, parentheses, or brackets that would
 * break the markdown link or its URL.
 */
export function attachmentMarkdown(path: string): string {
  return isImagePath(path) ? `![${path}](${path})` : `[${path}](${path})`;
}

/**
 * Build the message text delivered to the agent: the user's text followed by a
 * "See attachment here:" block whose values are the markdown (``attachmentMarkdown``)
 * for each attached file, one per line. Returns the text unchanged when there
 * are no attachments.
 */
export function buildMessageWithAttachments(text: string, attachmentPaths: readonly string[]): string {
  if (attachmentPaths.length === 0) {
    return text;
  }
  const prefix = attachmentPaths.length === 1 ? SINGLE_ATTACHMENT_PREFIX : MULTIPLE_ATTACHMENT_PREFIX;
  const block = prefix + attachmentPaths.map(attachmentMarkdown).join("\n");
  return text.length > 0 ? `${text}\n\n${block}` : block;
}

/**
 * Split a sent message's content into the user-visible text and the trailing
 * "See attachment here:" block built by ``buildMessageWithAttachments``. The
 * block is recognized only when every listed item references an uploads path,
 * so a user who merely types a similar sentence is never misread. The block is
 * returned verbatim (its values are markdown) for the bubble to render inline;
 * ``attachmentBlock`` is null when there is no block.
 */
export function parseMessageAttachments(content: string): { visibleText: string; attachmentBlock: string | null } {
  const match = content.match(ATTACHMENT_BLOCK_RE);
  if (match === null) {
    return { visibleText: content, attachmentBlock: null };
  }
  const items = match[2].split("\n").map((item) => item.trim());
  if (!items.every((item) => item.includes(UPLOADS_PATH_MARKER))) {
    return { visibleText: content, attachmentBlock: null };
  }
  const visibleText = content.slice(0, match.index ?? 0).replace(/\s+$/, "");
  return { visibleText, attachmentBlock: match[1] };
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

// --- Upload client ----------------------------------------------------------

interface AttachmentUploadResponseBody {
  path: string;
  size: number;
}

function makeUploadedAttachment(path: string, size: number): UploadedAttachment {
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
