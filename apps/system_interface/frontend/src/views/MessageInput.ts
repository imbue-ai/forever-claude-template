import m from "mithril";
import {
  clearComposerAttachments,
  getComposerAttachments,
  getReadyAttachmentPaths,
  hasReadyAttachments,
  removeComposerAttachment,
  restoreComposerAttachments,
  uploadFilesToComposer,
  waitForComposerUploads,
} from "../models/ComposerAttachments";
import type { ComposerAttachment } from "../models/ComposerAttachments";
import { buildMessageWithAttachments, formatFileSize } from "../models/attachments";
import { interruptAgent, sendMessage, getEventsForAgent } from "../models/Response";
import {
  addPendingMessage,
  getEffectiveActivityState,
  markPendingMessageQueued,
  removePendingMessage,
} from "../models/PendingMessages";
import { describeRequestError } from "../models/request-error";
import { isWorkingActivityState } from "./ActivityIndicator";

const MAX_TEXTAREA_HEIGHT_PX = 200;

const MESSAGE_TEXT_KEY_PREFIX = "message-text:";

const ATTACH_ICON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>';

const REMOVE_ICON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M18 6L6 18"/><path d="M6 6l12 12"/></svg>';

const FILE_ICON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M14 3v4a1 1 0 0 0 1 1h4"/>' +
  '<path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z"/></svg>';

function messageTextKey(agentId: string): string {
  return `${MESSAGE_TEXT_KEY_PREFIX}${agentId}`;
}

function autoResizeTextarea(textarea: HTMLTextAreaElement): void {
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, MAX_TEXTAREA_HEIGHT_PX)}px`;
  textarea.style.overflowY = textarea.scrollHeight > MAX_TEXTAREA_HEIGHT_PX ? "auto" : "hidden";
}

function imageFilesFromClipboard(clipboardData: DataTransfer | null): File[] {
  if (clipboardData === null) {
    return [];
  }
  const files: File[] = [];
  for (const item of Array.from(clipboardData.items)) {
    if (item.kind === "file") {
      const file = item.getAsFile();
      if (file !== null) {
        files.push(file);
      }
    }
  }
  return files;
}

// Compatibility export
export function setSelectedModelId(_modelId: string): void {}

export function MessageInput(): m.Component<{ agentId: string | null }> {
  let messageText = "";
  let currentAgentId: string | null = null;
  let messageTextareaElement: HTMLTextAreaElement | null = null;
  let fileInputElement: HTMLInputElement | null = null;
  let isInterruptInFlight = false;

  function focusMessageTextarea(): void {
    messageTextareaElement?.focus();
  }

  function renderComposerAttachment(agentId: string, attachment: ComposerAttachment): m.Vnode {
    const isReadyImage = attachment.status === "ready" && attachment.isImage && attachment.uploaded !== undefined;
    const thumbnail = isReadyImage
      ? m("img", {
          class: "composer-attachment-thumb",
          src: attachment.uploaded?.url,
          alt: attachment.fileName,
        })
      : m(
          "span",
          { class: "composer-attachment-icon" },
          attachment.status === "uploading"
            ? m("span", { class: "composer-attachment-spinner" })
            : m.trust(FILE_ICON_SVG),
        );
    return m(
      "div",
      { key: attachment.localId, class: `composer-attachment composer-attachment--${attachment.status}` },
      [
        thumbnail,
        m("span", { class: "composer-attachment-info" }, [
          m("span", { class: "composer-attachment-name", title: attachment.fileName }, attachment.fileName),
          attachment.status === "ready" && attachment.uploaded !== undefined
            ? m("span", { class: "composer-attachment-detail" }, formatFileSize(attachment.uploaded.size))
            : null,
          attachment.status === "uploading" ? m("span", { class: "composer-attachment-detail" }, "Uploading…") : null,
          attachment.status === "error"
            ? m("span", { class: "composer-attachment-detail composer-attachment-detail--error" }, "Upload failed")
            : null,
        ]),
        attachment.status === "uploading"
          ? null
          : m(
              "button",
              {
                type: "button",
                class: "composer-attachment-remove",
                title: "Remove attachment",
                "aria-label": "Remove attachment",
                onclick: () => removeComposerAttachment(agentId, attachment.localId),
              },
              m.trust(REMOVE_ICON_SVG),
            ),
      ],
    );
  }

  return {
    view(vnode) {
      const agentId = vnode.attrs.agentId;

      if (!agentId) {
        return null;
      }

      if (currentAgentId !== agentId) {
        currentAgentId = agentId;
        messageText = localStorage.getItem(messageTextKey(agentId)) ?? "";
        isInterruptInFlight = false;
      }

      async function handleSend(): Promise<void> {
        if (!agentId) {
          return;
        }
        // Wait for in-flight uploads so a just-dropped file is included rather
        // than dropped from the message.
        await waitForComposerUploads(agentId);

        const attachmentPaths = getReadyAttachmentPaths(agentId);
        const text = messageText;
        if (!text.trim() && attachmentPaths.length === 0) {
          return;
        }

        const finalText = buildMessageWithAttachments(text, attachmentPaths);
        // Snapshot for rollback if the send fails.
        const sentText = text;
        const sentAttachments = getComposerAttachments(agentId);

        messageText = "";
        clearComposerAttachments(agentId);
        localStorage.removeItem(messageTextKey(agentId));
        // Show the message immediately (and force "Thinking..." if the agent is
        // idle) instead of waiting for it to round-trip through the transcript.
        const pendingId = addPendingMessage(agentId, finalText, getEventsForAgent(agentId));
        m.redraw();

        try {
          await sendMessage(agentId, finalText);
          // The POST resolves once the backend confirms the agent accepted the
          // message into its queue, so move the bubble to "queued". It stays up
          // until the real transcript event reconciles it away -- that is when
          // the agent has genuinely received it (the user-facing "sent").
          if (pendingId !== null) {
            markPendingMessageQueued(agentId, pendingId);
          }
        } catch (err) {
          // The send genuinely failed (the backend confirms delivery before
          // resolving, so a rejection means the message was NOT accepted). Roll
          // the optimistic bubble back (clearing the forced-"Thinking..."
          // override) so the UI does not show a message that was never
          // delivered, and surface the real error.
          const detail = describeRequestError(err);
          console.error(`Failed to send message to agent ${agentId}: ${detail}`);
          if (pendingId !== null) {
            removePendingMessage(agentId, pendingId);
          }
          // Restore the user's text and attachments so the send is not silently
          // lost -- but only if they have not already started a new draft for
          // this agent (the input was cleared at send time, so during the
          // in-flight request the user may have typed or attached something
          // new; blindly restoring would clobber that newer draft).
          const currentDraft =
            currentAgentId === agentId ? messageText : (localStorage.getItem(messageTextKey(agentId)) ?? "");
          const isComposerEmpty = currentDraft.trim().length === 0 && getComposerAttachments(agentId).length === 0;
          if (isComposerEmpty) {
            localStorage.setItem(messageTextKey(agentId), sentText);
            restoreComposerAttachments(agentId, sentAttachments);
            if (currentAgentId === agentId) {
              messageText = sentText;
              m.redraw();
            }
          }
          // Surface the failure to the user with an explicit signal: the bubble
          // vanishing on its own is too subtle to read as "your message did not
          // send." Matches the alert-based feedback convention for user-initiated
          // mutations in this file (see handleInterrupt).
          alert(`Failed to send message: ${detail}`);
        }

        requestAnimationFrame(() => {
          focusMessageTextarea();
        });
      }

      async function handleInterrupt(): Promise<void> {
        if (!agentId || isInterruptInFlight) {
          return;
        }
        // Hide the stop button until the restart request settles so the user
        // cannot fire off multiple restarts in quick succession.
        isInterruptInFlight = true;
        m.redraw();
        try {
          await interruptAgent(agentId);
        } catch (err) {
          const detail = describeRequestError(err);
          console.error(`Failed to interrupt agent ${agentId}: ${detail}`);
          // Surface the failure to the user: they deliberately clicked Stop,
          // and on failure the agent is still running. Matches the alert-based
          // feedback convention for user-initiated mutations (see executeDestroy).
          alert(`Failed to interrupt agent: ${detail}`);
        } finally {
          isInterruptInFlight = false;
          m.redraw();
        }
      }

      function handleKeydown(event: KeyboardEvent): void {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          handleSend();
        }
      }

      function handlePaste(event: ClipboardEvent): void {
        if (!agentId) {
          return;
        }
        const files = imageFilesFromClipboard(event.clipboardData);
        if (files.length > 0) {
          event.preventDefault();
          uploadFilesToComposer(agentId, files);
        }
      }

      function openFilePicker(): void {
        fileInputElement?.click();
      }

      const attachments = getComposerAttachments(agentId);
      const hasMessageText = messageText.trim().length > 0;
      const canSend = hasMessageText || hasReadyAttachments(agentId);

      // The stop button is only meaningful while the agent has an interruptible
      // turn in progress -- the same condition that drives the activity
      // indicator above the input. Use the effective state so a just-sent
      // message that forced "Thinking..." also surfaces the stop button, keeping
      // the two in lockstep. Hide it whenever the agent is idle.
      const isAgentWorking = isWorkingActivityState(getEffectiveActivityState(agentId));
      const isStopButtonVisible = isAgentWorking && !isInterruptInFlight;

      return m("div", { class: "message-input mx-auto w-full" }, [
        m("input", {
          type: "file",
          multiple: true,
          class: "message-input-file-input",
          oncreate: (inputVnode: m.VnodeDOM) => {
            fileInputElement = inputVnode.dom as HTMLInputElement;
          },
          onremove: () => {
            fileInputElement = null;
          },
          onchange: (event: Event) => {
            const input = event.target as HTMLInputElement;
            uploadFilesToComposer(agentId, input.files);
            input.value = "";
          },
        }),
        m("div", { class: "message-input-box flex flex-col" }, [
          attachments.length > 0
            ? m(
                "div",
                { class: "message-input-attachments" },
                attachments.map((attachment) => renderComposerAttachment(agentId, attachment)),
              )
            : null,
          m("div", { class: "message-input-row flex flex-row items-center" }, [
            m("textarea", {
              class: "message-input-textbox flex-1 resize-none focus:outline-none",
              placeholder: "Type a message...",
              rows: 1,
              value: messageText,
              oncreate: (textareaVnode: m.VnodeDOM) => {
                messageTextareaElement = textareaVnode.dom as HTMLTextAreaElement;
                autoResizeTextarea(messageTextareaElement);
                focusMessageTextarea();
              },
              onupdate: (textareaVnode: m.VnodeDOM) => {
                messageTextareaElement = textareaVnode.dom as HTMLTextAreaElement;
                autoResizeTextarea(messageTextareaElement);
              },
              onremove: () => {
                messageTextareaElement = null;
              },
              oninput: (event: Event) => {
                const textarea = event.target as HTMLTextAreaElement;
                messageText = textarea.value;
                localStorage.setItem(messageTextKey(agentId), messageText);
                autoResizeTextarea(textarea);
              },
              onkeydown: handleKeydown,
              onpaste: handlePaste,
            }),
            m("div", { class: "message-input-toolbar" }, [
              m(
                "button",
                {
                  type: "button",
                  class: "message-input-attach-button",
                  title: "Attach files",
                  "aria-label": "Attach files",
                  onclick: openFilePicker,
                },
                m.trust(ATTACH_ICON_SVG),
              ),
              isStopButtonVisible
                ? m(
                    "button",
                    {
                      class: "message-input-stop-button",
                      title: "Interrupt current turn",
                      onclick: handleInterrupt,
                    },
                    m.trust(
                      '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
                    ),
                  )
                : null,
              canSend
                ? m(
                    "button",
                    {
                      class: "message-input-send-button",
                      onclick: handleSend,
                    },
                    m.trust(
                      '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>',
                    ),
                  )
                : null,
            ]),
          ]),
        ]),
      ]);
    },
  };
}
