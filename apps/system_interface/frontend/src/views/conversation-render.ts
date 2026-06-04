/**
 * Shared conversation renderer for the main chat and a subagent's conversation.
 *
 * Both views render the same way: a single in-order walk of the transcript
 * (buildSections) into turn sections, each shown as a progress timeline when it
 * has steps or as plain chat when it doesn't. Extracting it here means a
 * subagent's "View conversation" gets the real progress view -- step timeline,
 * statuses, summaries -- instead of raw tk Bash calls, with zero rendering
 * drift from the main chat.
 *
 * Callers pass already-visible events (any hiding, e.g. trimming the
 * pre-login auth-error prefix, is applied upstream) and the step enrichment
 * scoped to this conversation. `agentIsIdle` settles the frontier spinner on
 * the tail turn.
 */

import m from "mithril";
import type { TranscriptEvent, StepEnrichment } from "../models/Response";
import { renderUserMessage, renderAssistantMessage, buildToolResultsWithSkillExpansions } from "./message-renderers";
import { buildSections } from "./turn-grouping";
import { ProgressBlock } from "./ProgressBlock";

export function renderConversation(
  events: TranscriptEvent[],
  enrichment: Map<string, StepEnrichment>,
  agentId: string,
  agentIsIdle: boolean,
): m.Vnode {
  const toolResults = buildToolResultsWithSkillExpansions(events);
  const sections = buildSections(events, toolResults, enrichment, agentIsIdle);

  const messageNodes: m.Children[] = [];
  for (const section of sections) {
    if (section.user_event !== null) {
      const userNode = renderUserMessage(section.user_event);
      if (userNode !== null) messageNodes.push(userNode);
    }

    const hasSteps = section.items.some((i) => i.kind === "step");
    if (hasSteps) {
      messageNodes.push(
        m(ProgressBlock, {
          key: `progress-${section.key}`,
          items: section.items,
          trailing_reply: section.trailing_reply,
          toolResults,
          agentId,
        }),
      );
      continue;
    }

    // No steps this turn: render the body as plain chat -- prose and tool-call
    // blocks inline, items already in transcript order.
    for (const item of section.items) {
      if (item.kind === "ungrouped") {
        for (const e of item.events) messageNodes.push(renderAssistantMessage(e, toolResults, agentId));
      } else if (item.kind === "chip") {
        const chipNode = renderUserMessage(item.event);
        if (chipNode !== null) messageNodes.push(chipNode);
      }
    }
    for (const e of section.trailing_reply) messageNodes.push(renderAssistantMessage(e, toolResults, agentId));
  }

  return m("div", { class: "message-list-wrapper" }, [
    m("div", { class: "message-list mx-auto w-full max-w-(--width-message-column) flex flex-col py-6" }, messageNodes),
  ]);
}

/**
 * Whether a subagent is still running, used in place of the parent agent's
 * server-derived `activity_state` (which doesn't apply to a subagent). Minimal
 * by design: the subagent is running while its last assistant turn has no
 * terminal stop_reason (it's mid-tool-use or hasn't stopped); once it stops
 * with `end_turn`/`stop_sequence` it's settled. Drives whether the subagent's
 * frontier step may show a spinner.
 */
export function isSubagentRunning(events: TranscriptEvent[]): boolean {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.type === "assistant_message") {
      return event.stop_reason === null || event.stop_reason === "tool_use";
    }
  }
  return false;
}
