import m from "mithril";
import { apiUrl } from "../base-path";
import {
  applyEnrichmentSnapshot,
  getEnrichmentForAgent,
  type TranscriptEvent,
  type StepEnrichment,
  type SubagentMetadata,
} from "../models/Response";
import { renderConversation, isSubagentRunning } from "./conversation-render";

interface SubagentViewAttrs {
  agentId: string;
  subagentSessionId: string;
}

interface SubagentEventsResponse {
  events: TranscriptEvent[];
  metadata: SubagentMetadata | null;
  // The subagent's own steps, scoped to its session, so its conversation
  // renders a real progress timeline with the same code as the main chat.
  step_enrichment?: Record<string, StepEnrichment>;
}

export function SubagentView(): m.Component<SubagentViewAttrs> {
  let events: TranscriptEvent[] = [];
  let metadata: SubagentMetadata | null = null;
  let loading = true;
  let loadingError: string | null = null;
  let eventSource: EventSource | null = null;

  async function fetchSubagentEvents(agentId: string, subagentSessionId: string): Promise<void> {
    loading = true;
    loadingError = null;

    try {
      const result = await m.request<SubagentEventsResponse>({
        method: "GET",
        url: apiUrl(
          `/api/agents/${encodeURIComponent(agentId)}/subagents/${encodeURIComponent(subagentSessionId)}/events`,
        ),
      });
      events = result.events;
      metadata = result.metadata ?? null;
      applyEnrichmentSnapshot(agentId, result.step_enrichment, subagentSessionId);
      loading = false;
    } catch (error) {
      loading = false;
      loadingError = (error as Error).message ?? String(error);
    }
  }

  function connectToStream(agentId: string, subagentSessionId: string): void {
    if (eventSource !== null) {
      return;
    }

    const url = apiUrl(
      `/api/agents/${encodeURIComponent(agentId)}/subagents/${encodeURIComponent(subagentSessionId)}/stream`,
    );
    eventSource = new EventSource(url);

    eventSource.onmessage = (messageEvent: MessageEvent) => {
      const raw = JSON.parse(messageEvent.data) as { type?: string };
      // A step_enrichment message (tagged with this subagent's session id by
      // the backend) is a full enrichment snapshot, not a transcript event --
      // replace this subagent's table and redraw.
      if (raw.type === "step_enrichment") {
        const snapshot = raw as { enrichment?: Record<string, StepEnrichment> };
        applyEnrichmentSnapshot(agentId, snapshot.enrichment, subagentSessionId);
        m.redraw();
        return;
      }
      const event = raw as TranscriptEvent;
      const existingIds = new Set(events.map((e) => e.event_id));
      if (!existingIds.has(event.event_id)) {
        events = [...events, event];
        m.redraw();
      }
    };

    eventSource.onerror = () => {
      if (eventSource !== null) {
        eventSource.close();
        eventSource = null;
      }
    };
  }

  function disconnectFromStream(): void {
    if (eventSource !== null) {
      eventSource.close();
      eventSource = null;
    }
  }

  return {
    oninit(vnode) {
      const { agentId, subagentSessionId } = vnode.attrs;
      fetchSubagentEvents(agentId, subagentSessionId).then(() => {
        connectToStream(agentId, subagentSessionId);
      });
    },

    onremove() {
      disconnectFromStream();
    },

    view(vnode) {
      const { agentId, subagentSessionId } = vnode.attrs;
      const title = metadata?.description || "Sub-agent conversation";
      const agentType = metadata?.agent_type || "";

      const header = m("header", { class: "app-header" }, [
        m("h1", { class: "app-header-title" }, title),
        agentType ? m("span", { class: "app-header-model-badge" }, agentType) : null,
      ]);

      let content: m.Vnode;

      if (loading) {
        content = m(
          "div",
          { class: "message-list-loading flex items-center justify-center h-full" },
          m("p", { class: "text-text-secondary" }, "Loading events..."),
        );
      } else if (loadingError) {
        content = m(
          "div",
          { class: "message-list-error flex items-center justify-center h-full" },
          m("p", { class: "text-red-500" }, `Error: ${loadingError}`),
        );
      } else if (events.length === 0) {
        content = m(
          "div",
          { class: "message-list-empty flex items-center justify-center h-full" },
          m("p", { class: "text-text-secondary" }, "No events yet."),
        );
      } else {
        // Same renderer as the main chat. The subagent has no server-derived
        // activity_state, so derive idleness from the transcript tail.
        const enrichment = getEnrichmentForAgent(agentId, subagentSessionId);
        content = renderConversation(events, enrichment, agentId, !isSubagentRunning(events));
      }

      return m("div", { class: "app-content-wrapper flex-1 flex flex-col min-h-0" }, [
        header,
        m("main", { class: "app-content flex-1 overflow-y-auto px-8 py-6" }, content),
        // No footer/message input -- read-only
      ]);
    },
  };
}
