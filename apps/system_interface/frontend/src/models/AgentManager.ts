/**
 * Unified WebSocket-based agent and application state manager.
 * Receives real-time updates for agents, applications, and proto-agents.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

export interface AgentState {
  id: string;
  name: string;
  state: string;
  labels: Record<string, string>;
  work_dir: string | null;
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
  | { type: "refresh_service"; service_name: string };

export type RefreshServiceListener = (serviceName: string) => void;

let agents: AgentState[] = [];
let applications: ApplicationEntry[] = [];
let protoAgents: ProtoAgent[] = [];
let refreshListeners: RefreshServiceListener[] = [];
let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let connected = false;

const RECONNECT_DELAY_MS = 3000;

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
    m.redraw();
  };

  ws.onmessage = (event: MessageEvent) => {
    const data = JSON.parse(event.data as string) as WsEvent;
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
  }, RECONNECT_DELAY_MS);
}

function handleEvent(event: WsEvent): void {
  switch (event.type) {
    case "agents_updated":
      agents = event.agents;
      break;

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

    case "refresh_service":
      for (const listener of refreshListeners) {
        listener(event.service_name);
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

export function getAgents(): AgentState[] {
  return agents;
}

/**
 * Return the set of agents the user can chat with -- everything except the
 * system-services agent that owns this workspace_server. The system-services
 * agent runs `uv run bootstrap` (no chat transcript) and is identified by
 * `labels.is_primary === "true"`. The frontend filters it out of the
 * sidebar, the "+" menu, and the initial chat-tab selection.
 */
export function getChatAgents(): AgentState[] {
  return agents.filter((a) => !isSystemServicesAgent(a));
}

export function getAgentById(id: string): AgentState | undefined {
  return agents.find((a) => a.id === id);
}

/**
 * True when this agent is the system-services bootstrap agent rather than
 * a user-chattable agent. The contract is set by the FCT
 * `[create_templates.system_services]` template, which stamps
 * `is_primary=true` on every system-services agent.
 */
export function isSystemServicesAgent(agent: AgentState): boolean {
  return agent.labels?.is_primary === "true";
}

/**
 * Return the agent the ChatPanel should default-select on first load --
 * the agent named `assistant` (bootstrap creates one per workspace), or
 * the most recently-created non-system chat agent if `assistant` is gone.
 * Returns null when no chat agents exist yet.
 */
export function getDefaultChatAgentId(): string | null {
  const chatAgents = getChatAgents();
  if (chatAgents.length === 0) {
    return null;
  }
  const assistant = chatAgents.find((a) => a.name === "assistant");
  if (assistant) {
    return assistant.id;
  }
  // Fall back to the last entry in the agents list -- AgentManager preserves
  // arrival order, so "most recent" is the tail.
  return chatAgents[chatAgents.length - 1].id;
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

export function addRefreshServiceListener(listener: RefreshServiceListener): void {
  refreshListeners.push(listener);
}

export function removeRefreshServiceListener(listener: RefreshServiceListener): void {
  refreshListeners = refreshListeners.filter((l) => l !== listener);
}
