import m from "mithril";
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

function messageTextKey(agentId: string): string {
  return `${MESSAGE_TEXT_KEY_PREFIX}${agentId}`;
}

function autoResizeTextarea(textarea: HTMLTextAreaElement): void {
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, MAX_TEXTAREA_HEIGHT_PX)}px`;
  textarea.style.overflowY = textarea.scrollHeight > MAX_TEXTAREA_HEIGHT_PX ? "auto" : "hidden";
}

// Compatibility export
export function setSelectedModelId(_modelId: string): void {}

export function MessageInput(): m.Component<{ agentId: string | null }> {
  let messageText = "";
  let currentAgentId: string | null = null;
  let messageTextareaElement: HTMLTextAreaElement | null = null;
  let isInterruptInFlight = false;

  function focusMessageTextarea(): void {
    messageTextareaElement?.focus();
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
        if (!agentId || !messageText.trim()) {
          return;
        }

        const text = messageText;
        messageText = "";
        localStorage.removeItem(messageTextKey(agentId));
        // Show the message immediately (and force "Thinking..." if the agent is
        // idle) instead of waiting for it to round-trip through the transcript.
        const pendingId = addPendingMessage(agentId, text, getEventsForAgent(agentId));
        m.redraw();

        try {
          await sendMessage(agentId, text);
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
          // Restore the user's text so the send is not silently lost -- but only
          // if they have not already started a new draft for this agent. The
          // input was cleared at send time, so during the in-flight request the
          // user may have typed a fresh message; blindly restoring the failed
          // text would clobber that newer draft. The agent's current draft is
          // the live input when the user is still on this agent, otherwise its
          // persisted localStorage value.
          const currentDraft =
            currentAgentId === agentId ? messageText : (localStorage.getItem(messageTextKey(agentId)) ?? "");
          if (currentDraft.trim().length === 0) {
            // No newer draft to protect: recover the failed text. Persist it to
            // localStorage (keyed to this agent) so the recovered draft survives
            // a reload or agent switch, and only touch the live input if the user
            // is still on this agent (otherwise we would write into the input of
            // the agent they switched to; the draft stays recoverable here).
            localStorage.setItem(messageTextKey(agentId), text);
            if (currentAgentId === agentId) {
              messageText = text;
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

      const hasMessageText = messageText.trim().length > 0;

      // The stop button is only meaningful while the agent has an interruptible
      // turn in progress -- the same condition that drives the activity
      // indicator above the input. Use the effective state so a just-sent
      // message that forced "Thinking..." also surfaces the stop button, keeping
      // the two in lockstep. Hide it whenever the agent is idle.
      const isAgentWorking = isWorkingActivityState(getEffectiveActivityState(agentId));
      const isStopButtonVisible = isAgentWorking && !isInterruptInFlight;

      return m("div", { class: "message-input mx-auto w-full" }, [
        m("div", { class: "message-input-box flex flex-row items-center" }, [
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
          }),
          m("div", { class: "message-input-toolbar" }, [
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
            hasMessageText
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
      ]);
    },
  };
}
