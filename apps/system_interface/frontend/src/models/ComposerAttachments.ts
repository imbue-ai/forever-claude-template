/**
 * Per-agent store of the attachments the user has staged on the current draft.
 *
 * Files are uploaded to the agent VM as soon as they are dropped, pasted, or
 * picked, so each item moves from "uploading" to "ready" (or "error"). Held in a
 * model module rather than a view closure so both the composer (the attach
 * button / chips) and the chat panel (its panel-wide drop target) can stage the
 * same agent's attachments.
 */

import m from "mithril";
import { deleteAttachment } from "./attachments";
import { isImagePath } from "./attachments";
import { uploadAttachment } from "./attachments";
import type { UploadedAttachment } from "./attachments";
import { describeRequestError } from "./request-error";

export type ComposerAttachmentStatus = "uploading" | "ready" | "error";

export interface ComposerAttachment {
  /** Stable id for keying the rendered chip and addressing removals. */
  localId: string;
  /** Original filename, shown while uploading and on the chip. */
  fileName: string;
  /** Whether this is an image (drives thumbnail vs file chip), known up front
   *  from the dropped File so the chip can choose its shape before upload ends. */
  isImage: boolean;
  status: ComposerAttachmentStatus;
  /** Present once the upload succeeds. */
  uploaded?: UploadedAttachment;
  /** Present when the upload failed. */
  error?: string;
  /** Resolves when the upload settles (ready or error); awaited at send time so
   *  an in-flight upload is included rather than dropped. */
  uploadSettled?: Promise<void>;
}

let _nextLocalId = 0;
const _attachmentsByAgent: Record<string, ComposerAttachment[]> = {};

export function getComposerAttachments(agentId: string): ComposerAttachment[] {
  return _attachmentsByAgent[agentId] ?? [];
}

function _setAttachments(agentId: string, attachments: ComposerAttachment[]): void {
  _attachmentsByAgent[agentId] = attachments;
}

function _patchAttachment(agentId: string, localId: string, patch: Partial<ComposerAttachment>): void {
  const list = _attachmentsByAgent[agentId];
  if (list === undefined) {
    return;
  }
  _attachmentsByAgent[agentId] = list.map((item) => (item.localId === localId ? { ...item, ...patch } : item));
  m.redraw();
}

/**
 * Upload each dropped / pasted / picked file to the agent VM, staging it as a
 * composer attachment. Returns immediately; status transitions drive redraws.
 */
export function uploadFilesToComposer(agentId: string, files: FileList | readonly File[] | null | undefined): void {
  if (!files) {
    return;
  }
  const fileArray = Array.from(files);
  if (fileArray.length === 0) {
    return;
  }
  for (const file of fileArray) {
    const localId = `composer-att-${_nextLocalId++}`;
    const item: ComposerAttachment = {
      localId,
      fileName: file.name,
      isImage: file.type.startsWith("image/") || isImagePath(file.name),
      status: "uploading",
    };
    _setAttachments(agentId, [...getComposerAttachments(agentId), item]);
    item.uploadSettled = uploadAttachment(file)
      .then((uploaded) => {
        _patchAttachment(agentId, localId, { status: "ready", uploaded });
      })
      .catch((error: unknown) => {
        _patchAttachment(agentId, localId, { status: "error", error: describeRequestError(error) });
      });
  }
  m.redraw();
}

/**
 * Remove an attachment from the composer, deleting its VM copy so a removed file
 * does not linger on the agent (removal is treated as revoking access).
 */
export function removeComposerAttachment(agentId: string, localId: string): void {
  const list = getComposerAttachments(agentId);
  const item = list.find((attachment) => attachment.localId === localId);
  _setAttachments(
    agentId,
    list.filter((attachment) => attachment.localId !== localId),
  );
  m.redraw();
  if (item?.uploaded) {
    void deleteAttachment(item.uploaded.path);
  }
}

/** Await any in-flight uploads so their paths are available before sending. */
export async function waitForComposerUploads(agentId: string): Promise<void> {
  const pending = getComposerAttachments(agentId)
    .filter((attachment) => attachment.status === "uploading" && attachment.uploadSettled)
    .map((attachment) => attachment.uploadSettled as Promise<void>);
  if (pending.length > 0) {
    await Promise.allSettled(pending);
  }
}

export function getReadyAttachmentPaths(agentId: string): string[] {
  return getComposerAttachments(agentId)
    .filter((attachment) => attachment.status === "ready" && attachment.uploaded)
    .map((attachment) => (attachment.uploaded as UploadedAttachment).path);
}

export function hasReadyAttachments(agentId: string): boolean {
  return getComposerAttachments(agentId).some((attachment) => attachment.status === "ready");
}

/**
 * Clear the composer's attachments WITHOUT deleting the VM copies -- used after a
 * successful send, where the files are now referenced by the sent message.
 */
export function clearComposerAttachments(agentId: string): void {
  _setAttachments(agentId, []);
  m.redraw();
}

/** Restore a snapshot of attachments (used to roll back a failed send). */
export function restoreComposerAttachments(agentId: string, attachments: readonly ComposerAttachment[]): void {
  _setAttachments(agentId, [...attachments]);
  m.redraw();
}
