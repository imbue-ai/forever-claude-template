/**
 * Progress block: timeline rendering of one section (one user turn).
 *
 * The section is a flat, ordered list of timeline items produced by the
 * transcript walk (see turn-grouping): step nodes, ungrouped work/prose runs
 * (rendered inline, thread-breaking -- including a step's ejected closing
 * prose), and chips. Step nodes carry their own grouped events; expanding a step
 * reveals that grouped work. The wrap-up reply renders below the timeline.
 *
 * This component renders structure it is given; it does no grouping or
 * ordering itself.
 */

import m from "mithril";
import { MarkdownContent, renderMarkdown } from "../markdown";
import type { ToolResultEvent, AssistantMessageEvent } from "../models/Response";
import {
  renderAssistantMessage,
  renderAssistantMessageChildren,
  renderPermissionItem,
  renderUserMessage,
} from "./message-renderers";
import type { StepNode, StepStatus, TimelineItem } from "./turn-grouping";

interface ProgressBlockAttrs {
  /** Timeline items in transcript order (steps, ungrouped runs, chips). */
  items: TimelineItem[];
  /** The wrap-up reply, rendered below the timeline. */
  trailing_reply: AssistantMessageEvent[];
  /** Prebuilt tool_call_id -> tool_result map for the whole stream (skill
   *  expansions already folded in). Lookups by id work even though a section
   *  only references a subset. */
  toolResults: Map<string, ToolResultEvent>;
  agentId: string;
  /** Optional DOM id for the root, so a virtualized list can measure this
   *  block's height by querying ``.message-list > [id]``. */
  id?: string;
}

function statusIcon(status: StepStatus, is_frontier: boolean): m.Vnode {
  if (status === "done") {
    return m(
      "svg.pv-icon.pv-icon--done",
      { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none" },
      m.trust(
        '<circle cx="8" cy="8" r="7" fill="currentColor"/><path d="M4.5 8L7 10.5L11.5 6" stroke="white" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
      ),
    );
  }
  if (status === "active") {
    // The live frontier step spins; any other active step is settled (a
    // static partial ring) -- a past-turn carryover, an idle agent, or a step
    // superseded by a later one.
    if (!is_frontier) {
      return m(
        "svg.pv-icon.pv-icon--in-flight",
        { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none" },
        m.trust(
          '<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" opacity="0.35"/>' +
            '<path d="M8 2 A6 6 0 0 1 14 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>',
        ),
      );
    }
    return m("span.pv-icon.pv-icon--active", m("span.pv-spinner"));
  }
  return m(
    "svg.pv-icon.pv-icon--pending",
    { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none" },
    m.trust('<circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1" stroke-dasharray="2 2"/>'),
  );
}

/** Sub-caption under the step title:
 *   - done + summary -> the close summary
 *   - active + narration (and not expanded) -> latest in-step narration
 *   - otherwise nothing. */
function renderStepCaption(step: StepNode, isExpanded: boolean): m.Vnode | null {
  if (step.status === "done") {
    return step.summary ? m("div.pv-tl-summary", step.summary) : null;
  }
  if (isExpanded) return null;
  if (!step.narration) return null;
  const captionClass = step.is_frontier ? "pv-tl-narration" : "pv-tl-narration--static";
  return m(`div.${captionClass}.markdown-content`, m.trust(renderMarkdown(step.narration)));
}

function renderExpandedStepBody(step: StepNode, toolResults: Map<string, ToolResultEvent>, agentId: string): m.Vnode {
  const children: m.Children[] = [];
  for (const e of step.events) {
    children.push(...renderAssistantMessageChildren(e, toolResults, agentId));
  }
  return m("div.pv-expanded.markdown-content", children);
}

export function ProgressBlock(): m.Component<ProgressBlockAttrs> {
  // Per-step expand state, keyed by ticket_id. Each section mounts its own
  // ProgressBlock instance (keyed by section), so a carryover step rendered in
  // two turns holds independent expand state with no collision.
  const expanded = new Set<string>();

  function toggle(ticket_id: string): void {
    if (expanded.has(ticket_id)) expanded.delete(ticket_id);
    else expanded.add(ticket_id);
  }

  function renderStepNode(
    step: StepNode,
    is_last: boolean,
    toolResults: Map<string, ToolResultEvent>,
    agentId: string,
  ): m.Vnode {
    const canExpand = step.events.length > 0;
    const isExpanded = expanded.has(step.ticket_id);
    const nodeClasses = [
      "pv-tl-node",
      `pv-tl-node--${step.status}`,
      "pv-tl-node--step",
      is_last ? "pv-tl-node--last" : "",
    ]
      .filter(Boolean)
      .join(" ");

    return m("div", { class: nodeClasses, key: `step-${step.ticket_id}` }, [
      m("div.pv-tl-bullet", statusIcon(step.status, step.is_frontier)),
      m("div.pv-tl-body", [
        m(
          "button",
          {
            type: "button",
            class: "pv-tl-title",
            disabled: !canExpand,
            onclick: canExpand ? () => toggle(step.ticket_id) : undefined,
          },
          [
            step.title,
            canExpand
              ? m("span", { class: `pv-chev ${isExpanded ? "pv-chev--open" : ""}` }, m.trust("&rsaquo;"))
              : null,
          ],
        ),
        renderStepCaption(step, isExpanded),
        isExpanded ? m("div.pv-tl-expanded", renderExpandedStepBody(step, toolResults, agentId)) : null,
      ]),
    ]);
  }

  return {
    view(vnode) {
      const { items, trailing_reply, toolResults, agentId, id } = vnode.attrs;

      // Index of the last step item, so only it gets the `--last` thread cap.
      let lastStepIdx = -1;
      for (let i = 0; i < items.length; i++) if (items[i].kind === "step") lastStepIdx = i;

      const timelineNodes: m.Children[] = items.map((item, idx) => {
        if (item.kind === "step") {
          return renderStepNode(item.step, idx === lastStepIdx, toolResults, agentId);
        }
        if (item.kind === "ungrouped") {
          // Real work / prose that happened with no step open -- including a
          // step's ejected closing prose: rendered inline as a thread-breaking
          // block, exactly like a no-steps turn.
          return m(
            "div.pv-ungrouped",
            { key: item.key },
            item.events.map((e) => renderAssistantMessage(e, toolResults, agentId)),
          );
        }
        if (item.kind === "permission") {
          // A permission request lifted out of its step: rendered inline as a
          // thread-breaking block so it is always visible, as the
          // permission-request card the renderer produces (with its review button
          // or, once the user decides, a granted/denied verdict).
          return m(
            "div.pv-permission",
            { key: `perm-${item.event.event_id}` },
            renderPermissionItem(item.event, toolResults, agentId, item.resolution),
          );
        }
        // chip
        return m("div.pv-stophook", { key: `chip-${item.event.event_id}` }, renderUserMessage(item.event));
      });

      return m("div.progress-block", { id }, [
        m("div.pv.pv--timeline", [
          m("div.pv-timeline-thread", { "aria-hidden": "true" }),
          m("div.pv-timeline-nodes", timelineNodes),
        ]),
        trailing_reply.length > 0
          ? trailing_reply.map((ev) => m("div.pv-final", m(MarkdownContent, { content: ev.text ?? "" })))
          : null,
      ]);
    },
  };
}
