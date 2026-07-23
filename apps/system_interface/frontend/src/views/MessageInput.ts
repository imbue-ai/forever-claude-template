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
import {
  fetchModelSettings,
  getModelSettings,
  getSelectedOption,
  setFastMode,
  setModel,
} from "../models/ModelSettings";
import { isWorkingActivityState } from "./ActivityIndicator";
import { icon, stopIcon } from "./icons";

const MAX_TEXTAREA_HEIGHT_PX = 200;

const MESSAGE_TEXT_KEY_PREFIX = "message-text:";

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
  let isModelDropdownOpen = false;
  let modelSelectorElement: HTMLElement | null = null;

  function focusMessageTextarea(): void {
    messageTextareaElement?.focus();
  }

  // Stable reference (defined once for the component's life) so the dropdown's
  // add/removeEventListener pair to the same function -- a per-render closure
  // would leak a listener each time the dropdown reopens.
  function handleModelOutsideMousedown(event: MouseEvent): void {
    if (modelSelectorElement !== null && !modelSelectorElement.contains(event.target as Node)) {
      isModelDropdownOpen = false;
      m.redraw();
    }
  }

  // The model picker + fast-mode toggle live in the composer toolbar, alongside
  // the attach and stop/send buttons. The current selection is read from the
  // agent's Claude Code settings (fetched on agent switch); picking a model or
  // flipping fast mode posts a `/model` / `/fast` command that the running
  // session applies immediately (see ModelSettings.ts + server.py). The fast
  // toggle is an icon button that only appears for a model that supports fast
  // mode (Opus) and lights up while it is on. Returns the toolbar items (model
  // pill first, then the fast toggle when applicable) for the caller to place.
  function renderModelControls(agentId: string): m.Children[] {
    const settings = getModelSettings(agentId);
    const selected = getSelectedOption(agentId);
    const triggerLabel = selected?.label ?? "Model";

    const modelWrapper = m(
      "div",
      {
        class: "model-selector-wrapper",
        oncreate: (wrapperVnode: m.VnodeDOM) => {
          modelSelectorElement = wrapperVnode.dom as HTMLElement;
        },
        onremove: () => {
          modelSelectorElement = null;
        },
      },
      [
        m(
          "button",
          {
            type: "button",
            class: "model-selector-trigger",
            disabled: settings === null,
            "data-tooltip": "Select model",
            onclick: (event: MouseEvent) => {
              event.stopPropagation();
              isModelDropdownOpen = !isModelDropdownOpen;
            },
          },
          [
            m("span", { class: "model-selector-label" }, triggerLabel),
            m("span", { class: "model-selector-chevron" }, m.trust(icon("chevron-down", { size: 12 }))),
          ],
        ),
        isModelDropdownOpen && settings !== null
          ? m(
              "div",
              {
                class: "model-selector-dropdown",
                // Close on any click outside the picker while it is open.
                oncreate: () => document.addEventListener("mousedown", handleModelOutsideMousedown),
                onremove: () => document.removeEventListener("mousedown", handleModelOutsideMousedown),
              },
              [
                m("div", { class: "model-selector-dropdown-header" }, "Model"),
                m(
                  "ul",
                  { class: "model-selector-dropdown-list" },
                  settings.options.map((option) =>
                    m(
                      "li",
                      {
                        key: option.id,
                        class:
                          "model-selector-option" +
                          (selected?.id === option.id ? " model-selector-option--selected" : ""),
                        onclick: () => {
                          isModelDropdownOpen = false;
                          if (selected?.id !== option.id) {
                            setModel(agentId, option.id);
                          }
                        },
                      },
                      option.label,
                    ),
                  ),
                ),
              ],
            )
          : null,
      ],
    );

    const fastToggle =
      settings !== null && settings.fast_mode_supported
        ? m(
            "button",
            {
              type: "button",
              class: `fast-toggle${settings.fast_mode ? " fast-toggle--on" : ""}`,
              "data-tooltip": settings.fast_mode ? "Disable fast mode" : "Enable fast mode",
              "aria-label": settings.fast_mode ? "Disable fast mode" : "Enable fast mode",
              "aria-pressed": settings.fast_mode ? "true" : "false",
              onclick: () => setFastMode(agentId, !settings.fast_mode),
            },
            m.trust(icon("zap", { size: 16 })),
          )
        : null;

    return [modelWrapper, fastToggle];
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
            : m.trust(icon("file", { size: 18, strokeWidth: 1.8 })),
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
              m.trust(icon("close", { size: 12, strokeWidth: 2.5 })),
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
        isModelDropdownOpen = false;
        // Load this agent's model + fast-mode selection for the picker (cached
        // per agent, so this is a no-op once loaded).
        fetchModelSettings(agentId);
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
              ...renderModelControls(agentId),
              m(
                "button",
                {
                  type: "button",
                  class: "message-input-attach-button",
                  "data-tooltip": "Attach files",
                  "aria-label": "Attach files",
                  onclick: openFilePicker,
                },
                m.trust(icon("attach", { size: 18 })),
              ),
              isStopButtonVisible
                ? m(
                    "button",
                    {
                      class: "message-input-stop-button",
                      "data-tooltip": "Interrupt",
                      "aria-label": "Interrupt",
                      onclick: handleInterrupt,
                    },
                    m.trust(stopIcon(14)),
                  )
                : null,
              canSend
                ? m(
                    "button",
                    {
                      class: "message-input-send-button",
                      "data-tooltip": "Send message",
                      "aria-label": "Send message",
                      onclick: handleSend,
                    },
                    m.trust(icon("send", { size: 16, strokeWidth: 2.5 })),
                  )
                : null,
            ]),
          ]),
        ]),
      ]);
    },
  };
}
