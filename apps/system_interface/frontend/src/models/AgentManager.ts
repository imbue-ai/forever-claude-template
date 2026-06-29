/**
 * Unified WebSocket-based agent and application state manager.
 * Receives real-time updates for agents, applications, and proto-agents.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { ReconnectBackoff } from "./backoff";
import { parseJsonMessage } from "./ws-json";

export interface AgentState {
  id: string;
  name: string;
  state: string;
  labels: Record<string, string>;
  work_dir: string | null;
  // Per-agent chat activity. THINKING/TOOL_RUNNING/IDLE, or null when the
  // system interface has no per-agent activity tracking available (e.g.
  // remote agents whose state directory is not present on this host,
  // proto-agents, non-Claude agent types).
  activity_state?: string | null;
}

export interface ApplicationEntry {
  name: string;
  url: string;
}

export interface ProtoAgent {
  agent_id: string;
  name: string;
  creation_type: "worktree" | "chat";
  parent_agent_id: string | null;
}

// Names of the layout-mutation ops the agent-facing ``scripts/layout.py``
// helper can emit. The frontend dispatches on this in DockviewWorkspace.
export type LayoutOpName =
  | "open"
  | "focus"
  | "split"
  | "close"
  | "move"
  | "rename"
  | "maximize"
  | "restore"
  | "replace-url"
  | "refresh"
  | "reload_system_interface";

export interface LayoutOpEvent {
  op: LayoutOpName;
  // Op-specific arguments. Shape is verified at the call site (DockviewWorkspace)
  // rather than at the listener boundary -- the WS broadcast is the source of
  // truth and ``scripts/layout.py`` enforces shape before broadcasting.
  args: Record<string, unknown>;
  // ``MNGR_AGENT_ID`` of the agent that invoked ``scripts/layout.py``. Empty
  // string when the caller did not set ``MNGR_AGENT_ID``. Used to anchor
  // splits against the requester's own chat panel and to resolve the ``self``
  // ref.
  requesterAgentId: string;
}

type WsEvent =
  | { type: "agents_updated"; agents: AgentState[] }
  | { type: "applications_updated"; applications: ApplicationEntry[] }
  | {
      type: "proto_agent_created";
      agent_id: string;
      name: string;
      creation_type: string;
      parent_agent_id: string | null;
    }
  | { type: "proto_agent_completed"; agent_id: string; success: boolean; error: string | null }
  | {
      type: "layout_op";
      op: LayoutOpName;
      args: Record<string, unknown>;
      requester_agent_id?: string;
    };

export type LayoutOpListener = (event: LayoutOpEvent) => void;
export type AgentsUpdatedListener = (agents: AgentState[]) => void;
/**
 * Notified when a single agent's ``activity_state`` changes between two
 * consecutive ``agents_updated`` snapshots. ``previous`` is ``null`` when the
 * agent had no prior tracked state (it just appeared, or its state was
 * untracked). Computed here, in the agent-state authority, so consumers can act
 * on a transition (e.g. working -> IDLE) without keeping their own shadow copy
 * of the previous state.
 */
export type AgentActivityListener = (agentId: string, previous: string | null, current: string | null) => void;

let agents: AgentState[] = [];
let applications: ApplicationEntry[] = [];
let protoAgents: ProtoAgent[] = [];
let layoutOpListeners: LayoutOpListener[] = [];
let agentsUpdatedListeners: AgentsUpdatedListener[] = [];
let agentActivityListeners: AgentActivityListener[] = [];
let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let connected = false;

const reconnectBackoff = new ReconnectBackoff();

function getWsUrl(): string {
  const base = apiUrl("/api/ws");
  const loc = window.location;
  const protocol = loc.protocol === "https:" ? "wss:" : "ws:";
  if (base.startsWith("http")) {
    return base.replace(/^http/, "ws");
  }
  return `${protocol}//${loc.host}${base}`;
}

function connect(): void {
  if (ws !== null) {
    return;
  }

  const url = getWsUrl();
  ws = new WebSocket(url);

  ws.onopen = () => {
    connected = true;
    // A successful connection resets the backoff so the next disconnect
    // starts from the base delay again.
    reconnectBackoff.reset();
    m.redraw();
  };

  ws.onmessage = (event: MessageEvent) => {
    const data = parseJsonMessage<WsEvent>(event.data as string);
    if (data === null) {
      return;
    }
    handleEvent(data);
    m.redraw();
  };

  ws.onclose = () => {
    ws = null;
    connected = false;
    scheduleReconnect();
    m.redraw();
  };

  ws.onerror = () => {
    ws?.close();
  };
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null) {
    return;
  }
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, reconnectBackoff.nextDelay());
}

function handleEvent(event: WsEvent): void {
  switch (event.type) {
    case "agents_updated": {
      // Diff against the outgoing snapshot (still in `agents` here) so we can
      // report per-agent activity transitions before replacing it. No separate
      // previous-state bookkeeping is needed -- the prior array is the record.
      const previousActivityById = new Map(agents.map((a) => [a.id, a.activity_state ?? null]));
      agents = event.agents;
      for (const listener of agentsUpdatedListeners) {
        listener(getAgents());
      }
      for (const agent of agents) {
        const current = agent.activity_state ?? null;
        const previous = previousActivityById.get(agent.id) ?? null;
        if (previous !== current) {
          for (const listener of agentActivityListeners) {
            listener(agent.id, previous, current);
          }
        }
      }
      break;
    }

    case "applications_updated":
      applications = event.applications;
      break;

    case "proto_agent_created":
      protoAgents.push({
        agent_id: event.agent_id,
        name: event.name,
        creation_type: event.creation_type as "worktree" | "chat",
        parent_agent_id: event.parent_agent_id,
      });
      break;

    case "proto_agent_completed": {
      protoAgents = protoAgents.filter((p) => p.agent_id !== event.agent_id);
      break;
    }

    case "layout_op":
      for (const listener of layoutOpListeners) {
        listener({
          op: event.op,
          args: event.args,
          requesterAgentId: event.requester_agent_id ?? "",
        });
      }
      break;
  }
}

export function initAgentManager(): void {
  connect();
}

export function isConnected(): boolean {
  return connected;
}

/**
 * Returns true when the agent is the workspace's services-only "primary"
 * agent (window 0 is sleep-infinity; bootstrap + services run in extra
 * tmux windows). These agents are hidden from the user-facing agent list
 * because destroying them would tear down the whole workspace.
 */
export function isPrimaryAgent(agent: AgentState): boolean {
  return agent.labels?.is_primary === "true";
}

export function getAgents(): AgentState[] {
  // Filter at the data layer so every consumer (Dockview list, chat panel,
  // create-agent modal, etc.) sees the same set without duplicating the
  // filter logic. The raw list is still kept internally for callsites that
  // need it (none today, but kept symmetric with getAgentById).
  return agents.filter((a) => !isPrimaryAgent(a));
}

export function getAgentById(id: string): AgentState | undefined {
  return agents.find((a) => a.id === id);
}

export function removeAgentLocally(agentId: string): void {
  agents = agents.filter((a) => a.id !== agentId);
}

export function getApplications(): ApplicationEntry[] {
  return applications;
}

export function getProtoAgents(): ProtoAgent[] {
  return protoAgents;
}

export function addLayoutOpListener(listener: LayoutOpListener): void {
  layoutOpListeners.push(listener);
}

export function removeLayoutOpListener(listener: LayoutOpListener): void {
  layoutOpListeners = layoutOpListeners.filter((l) => l !== listener);
}

export function addAgentsUpdatedListener(listener: AgentsUpdatedListener): void {
  agentsUpdatedListeners.push(listener);
}

export function removeAgentsUpdatedListener(listener: AgentsUpdatedListener): void {
  agentsUpdatedListeners = agentsUpdatedListeners.filter((l) => l !== listener);
}

export function addAgentActivityListener(listener: AgentActivityListener): void {
  agentActivityListeners.push(listener);
}

export function removeAgentActivityListener(listener: AgentActivityListener): void {
  agentActivityListeners = agentActivityListeners.filter((l) => l !== listener);
}
