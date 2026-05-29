/**
 * Progress block: Timeline rendering of the agent's tk-tracked steps.
 * Each step is a node on a vertical thread, with status icon + title +
 * (when done) summary.
 *
 * Each step can be expanded via its chevron to reveal the raw assistant
 * text + tool_call_blocks that occurred during the step's active window.
 */

import m from "mithril";
import { MarkdownContent, renderMarkdown } from "../markdown";
import type { TranscriptEvent } from "../models/Response";
import { renderAssistantMessageChildren } from "./message-renderers";
import type { TaskInTurn, TaskUiStatus } from "./turn-grouping";
import { eventsInTaskWindow } from "./turn-grouping";

interface ProgressBlockAttrs {
  tasks: TaskInTurn[];
  body_events: TranscriptEvent[];
  /** Prebuilt tool_call_id -> tool_result map for the WHOLE event stream,
   *  with skill-expansion user_messages already folded into their
   *  matching Skill tool call. ChatPanel builds this once per redraw and
   *  threads it through so that expanding a task doesn't trigger a
   *  per-task O(n log n) rebuild of the same map. Lookups by id work
   *  fine even though only a subset of events is in this turn. */
  toolResults: Map<string, TranscriptEvent>;
  /** Text-only assistant messages from this turn (in chronological order)
   *  that should appear at the top level rather than buried inside a
   *  task's expanded panel. ChatPanel selects assistant_messages with
   *  non-empty text and no tool_calls; together they cover both:
   *    - the agent's "between tasks" or "after all tasks" prose, and
   *    - the agent's final reply when a task was left open at turn end
   *      (which would otherwise land inside the open task's window and
   *      be hidden in its dropdown).
   *  Rendered as separate blocks below the Timeline in arrival order so
   *  no substantive text gets dropped. */
  final_messages: TranscriptEvent[];
  agentId: string;
}

function statusIcon(status: TaskUiStatus, is_settled: boolean): m.Vnode {
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
    // Settled variant: the step is no longer actively being worked on
    // (either in a past partition or the agent is idle). Static partial
    // ring instead of a spinner.
    if (is_settled) {
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

/** Single sub-caption under the task title:
 *   - done + summary    -> render the close summary (even when expanded)
 *   - done + no summary -> render nothing (clean final state)
 *   - active + expanded -> hide narration (the expanded panel already shows it)
 *   - active            -> render the latest in-window narration, if any
 *   - pending           -> render nothing (no window yet)
 */
function renderTaskCaption(task: TaskInTurn, isExpanded: boolean): m.Vnode | null {
  if (task.status === "done") {
    return task.summary ? m("div.pv-tl-summary", task.summary) : null;
  }
  if (isExpanded) return null;
  return task.narration ? m("div.pv-tl-narration.markdown-content", m.trust(renderMarkdown(task.narration))) : null;
}

function renderExpandedTaskBody(
  events: TranscriptEvent[],
  toolResults: Map<string, TranscriptEvent>,
  agentId: string,
): m.Vnode {
  // Callers must only mount this when there are events to render
  // (ProgressBlock guards on canExpand = taskEvents.length > 0).
  // toolResults is the full prebuilt map from ChatPanel; lookups by
  // tool_call_id work fine even though `events` is only this task's
  // window.

  // Reuse renderAssistantMessageChildren so the expanded panel renders
  // assistant text + tool calls identically to the rest of the chat
  // (interleaved markdown + tool-call-block chrome). Text-only messages
  // also belong here: they're part of the task's history. The most
  // recent one additionally surfaces as the always-visible narration
  // slot under the task title -- some small duplication for the latest
  // entry is fine, and earlier text-only messages would otherwise
  // disappear entirely from view.
  const children: m.Children[] = [];
  for (const e of events) {
    if (e.type !== "assistant_message") continue;
    children.push(...renderAssistantMessageChildren(e, toolResults, agentId));
  }

  return m("div.pv-expanded.markdown-content", children);
}

export function ProgressBlock(): m.Component<ProgressBlockAttrs> {
  // Per-task expand state, keyed by ticket_id. Reset across instances
  // so each turn's progress block has its own state.
  const expanded = new Set<string>();

  function toggle(ticket_id: string): void {
    if (expanded.has(ticket_id)) {
      expanded.delete(ticket_id);
    } else {
      expanded.add(ticket_id);
    }
  }

  function renderTaskNode(
    task: TaskInTurn,
    options: {
      is_last: boolean;
      is_child: boolean;
      body_events: TranscriptEvent[];
      toolResults: Map<string, TranscriptEvent>;
      agentId: string;
      tasks: TaskInTurn[];
    },
  ): m.Vnode {
    const { is_last, is_child, body_events, toolResults, agentId, tasks } = options;
    const taskEvents = eventsInTaskWindow(task, body_events, tasks);
    // A task is "expandable" when its window contains any assistant
    // content -- tool calls or plain text. Text-only messages also
    // render in the expanded panel (the latest one additionally
    // surfaces as the always-visible narration slot), so an
    // assistant_message of either flavour is enough to warrant
    // expand.
    const canExpand = taskEvents.some(
      (e) => e.type === "assistant_message" && (!!(e.tool_calls && e.tool_calls.length > 0) || !!e.text),
    );
    const isExpanded = expanded.has(task.ticket_id);
    // The kind class drives the chrome difference between a regular
    // ticket and a step record: tickets get a heavier title + an id
    // badge; steps render slimmer / de-emphasized. The child class
    // adds the indent rail for nested step children. is_last applies
    // to standalone nodes (and to the last sibling among children
    // separately, controlled by the children renderer).
    const kindClass = task.is_step ? "pv-tl-node--step" : "pv-tl-node--ticket";
    const nodeClasses = [
      "pv-tl-node",
      `pv-tl-node--${task.status}`,
      kindClass,
      is_child ? "pv-tl-node--child" : "",
      is_last ? "pv-tl-node--last" : "",
    ]
      .filter(Boolean)
      .join(" ");

    return m("div", { class: nodeClasses, key: task.ticket_id + (is_child ? "-c" : "") }, [
      m("div.pv-tl-bullet", statusIcon(task.status, task.is_settled)),
      m("div.pv-tl-body", [
        m(
          "button",
          {
            type: "button",
            class: "pv-tl-title",
            disabled: !canExpand,
            onclick: canExpand ? () => toggle(task.ticket_id) : undefined,
          },
          [
            // Id badge for regular tickets only -- gives the user a
            // visible handle on which tk ticket this row corresponds
            // to (matches the `tk show <id>` partial-id lookup). Steps
            // are agent-private and don't need it.
            !task.is_step
              ? m("span.pv-tl-id-badge", { title: `Ticket ${task.ticket_id}` }, `[${task.ticket_id}]`)
              : null,
            task.title,
            task.is_settled && task.status !== "done"
              ? m("span.pv-carryover-tag", { title: "This step is no longer actively being worked on" }, "settled")
              : null,
            canExpand
              ? m("span", { class: `pv-chev ${isExpanded ? "pv-chev--open" : ""}` }, m.trust("&rsaquo;"))
              : null,
          ],
        ),
        renderTaskCaption(task, isExpanded),
        isExpanded ? m("div.pv-tl-expanded", renderExpandedTaskBody(taskEvents, toolResults, agentId)) : null,
        // Nested step children (only for parent tickets in practice).
        task.children.length > 0
          ? m(
              "div.pv-tl-children",
              task.children.map((child, ci) =>
                renderTaskNode(child, {
                  is_last: ci === task.children.length - 1,
                  is_child: true,
                  body_events,
                  toolResults,
                  agentId,
                  tasks,
                }),
              ),
            )
          : null,
      ]),
    ]);
  }

  return {
    view(vnode) {
      const { tasks, body_events, toolResults, final_messages, agentId } = vnode.attrs;
      if (tasks.length === 0) {
        // Defensive: callers should not mount ProgressBlock when there
        // are no tasks. Fall back to no-op.
        return null;
      }

      // Mithril 2 enforces that all children of a fragment are either all
      // keyed or all unkeyed; the timeline-thread div is unkeyed while
      // the task nodes carry per-ticket keys for stable expand state.
      // Putting the keyed task vnodes inside their own container keeps
      // them a homogeneous (all-keyed) fragment and lets the unkeyed
      // thread sit next to them without violating the rule.
      const taskNodes = tasks.map((task, idx) =>
        renderTaskNode(task, {
          is_last: idx === tasks.length - 1,
          is_child: false,
          body_events,
          toolResults,
          agentId,
          tasks,
        }),
      );

      return m("div.progress-block", [
        m("div.pv.pv--timeline", [
          m("div.pv-timeline-thread", { "aria-hidden": "true" }),
          m("div.pv-timeline-nodes", taskNodes),
        ]),
        final_messages.length > 0
          ? final_messages.map((ev) => m("div.pv-final", m(MarkdownContent, { content: ev.text ?? "" })))
          : null,
      ]);
    },
  };
}
