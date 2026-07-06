/**
 * Shared row builder for the main chat and a subagent's conversation.
 *
 * Both views render the same way: a single in-order walk of the transcript
 * (buildSections) into turn sections, flattened into the virtualized list's
 * top-level rows (buildRows) -- a user message, a whole ProgressBlock for a turn
 * that has tk steps, an ungrouped assistant message, a stop-hook chip, or a
 * trailing wrap-up reply. Sharing it here means a subagent's "View conversation"
 * gets the real progress view -- step timeline, statuses, summaries -- and the
 * same windowed virtualization as the main chat, with zero rendering drift.
 *
 * Structure and decoration both come from the transcript walk (tk prints its
 * step decoration on stdout, which buildSections parses); there is no
 * side-channel enrichment. `agentIsIdle` settles the frontier spinner on the
 * tail turn. The pre-login auth-error prefix is hidden here (a no-op for a
 * subagent, which never has one) so the two views stay byte-identical.
 */

import m from "mithril";
import type { TranscriptEvent, ToolResultEvent } from "../models/Response";
import {
  renderUserMessage,
  renderAssistantMessage,
  renderPermissionItem,
  buildToolResultsWithSkillExpansions,
  computeAuthErrorHiddenEventIds,
} from "./message-renderers";
import { isHiddenUserMessage } from "./message-classification";
import { buildSections, type SectionView } from "./turn-grouping";
import { ProgressBlock } from "./ProgressBlock";
import { ESTIMATED_USER_HEIGHT_PX, ESTIMATED_ASSISTANT_HEIGHT_PX } from "./row-measurement";
import type { WindowSegment } from "../models/virtualWindow";

// Fallback height for a progress block until it has been measured. The user and
// assistant estimates are shared (see row-measurement).
export const ESTIMATED_PROGRESS_HEIGHT_PX = 360;

export interface RowDescriptor {
  key: string;
  estimate: number;
  // m.Children (not m.Vnode) because a row can be a component vnode
  // (ProgressBlock), whose typed attrs do not fit the bare Vnode<{}, {}>.
  render: () => m.Children;
}

/**
 * Render the ordered window segments (from computeTranscriptSlices) into the
 * message list's children: a spacer div for each spacer, and each row's own vnode
 * for each row-run. Shared by ChatPanel and SubagentView so both virtualize
 * identically.
 *
 * Spacers carry `overflow-anchor: none` so native scroll anchoring never picks a
 * spacer (whose height changes as rows page in/measure) as its anchor -- it anchors
 * to a real message row instead. They are keyed by role (top / mid / bottom) so the
 * key stays stable as the middle spacer appears and disappears with a disjoint
 * selection pin.
 */
export function renderTranscriptSegments(rows: RowDescriptor[], segments: WindowSegment[]): m.Children[] {
  const children: m.Children[] = [];
  for (let s = 0; s < segments.length; s++) {
    const segment = segments[s];
    if (segment.kind === "spacer") {
      const role = s === 0 ? "top" : s === segments.length - 1 ? "bottom" : "mid";
      children.push(
        m("div", { key: `__spacer_${role}`, style: `height: ${segment.height}px; overflow-anchor: none` }),
      );
    } else {
      for (let i = segment.startIndex; i < segment.endIndex; i++) {
        children.push(rows[i].render());
      }
    }
  }
  return children;
}

/**
 * Flatten the turn-grouped sections into the virtualized list's top-level rows.
 *
 * Each row is one mounted node in the message list. Keeping the grouping here
 * (rather than virtualizing raw events) preserves turn structure, the progress
 * timeline, skill expansions and auth-error hiding while still mounting only the
 * windowed rows. Render closures are invoked lazily so off-window rows never
 * build their vnodes (so MarkdownContent is only parsed for on-screen rows).
 * Every row's rendered root carries a DOM ``id`` equal to its ``key`` so
 * measureRows can read its height.
 */
function buildRows(
  agentId: string,
  sections: SectionView[],
  toolResults: Map<string, ToolResultEvent>,
): RowDescriptor[] {
  const rows: RowDescriptor[] = [];
  for (const section of sections) {
    const userEvent = section.user_event;
    if (userEvent !== null && !isHiddenUserMessage(userEvent.content || "")) {
      rows.push({
        key: userEvent.event_id,
        estimate: ESTIMATED_USER_HEIGHT_PX,
        render: () => renderUserMessage(userEvent) as m.Vnode,
      });
    }

    const hasSteps = section.items.some((i) => i.kind === "step");
    if (hasSteps) {
      const key = `progress-${section.key}`;
      rows.push({
        key,
        estimate: ESTIMATED_PROGRESS_HEIGHT_PX,
        render: () =>
          m(ProgressBlock, {
            id: key,
            key,
            items: section.items,
            trailing_reply: section.trailing_reply,
            toolResults,
            agentId,
          }),
      });
      continue;
    }

    // No steps this turn: render the body as plain chat -- prose and tool-call
    // blocks inline, the same as assistant messages outside a progress section.
    for (const item of section.items) {
      if (item.kind === "ungrouped") {
        for (const event of item.events) {
          rows.push({
            key: event.event_id,
            estimate: ESTIMATED_ASSISTANT_HEIGHT_PX,
            render: () => renderAssistantMessage(event, toolResults, agentId),
          });
        }
      } else if (item.kind === "permission") {
        // A permission request lifted out of its step: rendered inline as an
        // always-visible card so the user can act on it without expanding a step.
        const permissionEvent = item.event;
        const resolution = item.resolution;
        const permKey = `perm-${permissionEvent.event_id}`;
        rows.push({
          key: permKey,
          estimate: ESTIMATED_ASSISTANT_HEIGHT_PX,
          // Pass the row key as the DOM id so the measured height is cached under
          // the same key the window math looks up (see renderPermissionItem).
          render: () => renderPermissionItem(permissionEvent, toolResults, agentId, resolution, permKey),
        });
      } else if (item.kind === "chip") {
        const chipEvent = item.event;
        if (!isHiddenUserMessage(chipEvent.content || "")) {
          rows.push({
            key: chipEvent.event_id,
            estimate: ESTIMATED_USER_HEIGHT_PX,
            render: () => renderUserMessage(chipEvent) as m.Vnode,
          });
        }
      }
    }
    for (const event of section.trailing_reply) {
      rows.push({
        key: event.event_id,
        estimate: ESTIMATED_ASSISTANT_HEIGHT_PX,
        render: () => renderAssistantMessage(event, toolResults, agentId),
      });
    }
  }
  return rows;
}

/**
 * The full events -> virtualized rows pipeline shared by both conversation
 * views: hide the pre-login auth-error prefix, walk the transcript into turn
 * sections, then flatten into top-level rows. The structure and decoration --
 * which steps exist, their order, grouping, titles, summaries -- come purely
 * from the transcript walk.
 */
export function buildConversationRows(
  agentId: string,
  events: TranscriptEvent[],
  agentIsIdle: boolean,
): RowDescriptor[] {
  const toolResults = buildToolResultsWithSkillExpansions(events);
  const hiddenEventIds = computeAuthErrorHiddenEventIds(events);
  const visibleEvents = hiddenEventIds.size > 0 ? events.filter((e) => !hiddenEventIds.has(e.event_id)) : events;
  const sections = buildSections(visibleEvents, toolResults, agentIsIdle);
  return buildRows(agentId, sections, toolResults);
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
