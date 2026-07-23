/**
 * Unified WebSocket-based agent and application state manager.
 * Receives real-time updates for agents, applications, and proto-agents.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { ReconnectBackoff } from "./backoff";
import { getActiveLayoutSlug, getClientId, getDeviceKind } from "./ClientIdentity";
import { parseJsonMessage } from "./ws-json";

export interface AgentState {
  id: string;
  name: string;
  state: string;
  labels: Record<string, string>;
  work_dir: string | null;
  // The agent's harness ("claude", "codex", ...), from the backend. Routes the
  // harness-specific activity caption. Defaults to "claude" if absent.
  harness?: string;
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

// A live tmux terminal session (any tmux session whose name does NOT start
// with the mngr agent prefix). tmux is the source of truth for terminals:
// these are enumerated straight from ``tmux ls`` by the backend, so a session
// created from the UI, an agent, or a raw ``tmux new-session`` all show up
// identically here.
export interface TerminalSessionInfo {
  session_name: string;
  // The immutable tmux ``#{session_id}`` (e.g. ``$3``); survives a rename, so
  // it's the stable key for reflecting a renamed session back onto its tab.
  session_id: string;
  cwd: string;
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
    }
  | {
      // A terminal tab's underlying tmux session changed: the client attached
      // to a different session (``terminal_id`` set, tmux client-session-changed
      // hook) or a session was renamed (``terminal_id`` null, tmux
      // session-renamed hook -- match on ``session_id`` instead).
      type: "terminal_session";
      terminal_id: string | null;
      session_id: string;
      session_name: string;
    }
  | {
      // A named layout's content was saved (by any client). Clients with the
      // layout active (other than the saver) re-apply it; everyone refreshes
      // their cached layouts list.
      type: "layout_saved";
      layout_slug: string;
      display_name: string;
      saved_by_client_id: string;
    }
  | {
      // A named layout was deleted; clients with it active switch to the
      // fallback.
      type: "layout_deleted";
      layout_slug: string;
      fallback_layout_slug: string;
    }
  | {
      // An agent asked a client (or all clients, target null) to switch to a
      // named layout so subsequent layout ops can target it.
      type: "load_layout";
      layout_slug: string;
      display_name: string;
      target_client_id: string | null;
    };

/** Layout registry / sync events pushed over the WebSocket. */
export type LayoutSyncEvent =
  | { kind: "saved"; layoutSlug: string; displayName: string; savedByClientId: string }
  | { kind: "deleted"; layoutSlug: string; fallbackLayoutSlug: string }
  | { kind: "load"; layoutSlug: string; displayName: string; targetClientId: string | null };

export type LayoutSyncListener = (event: LayoutSyncEvent) => void;

export type LayoutOpListener = (event: LayoutOpEvent) => void;
export type AgentsUpdatedListener = (agents: AgentState[]) => void;
/**
 * Notified when a terminal tab's underlying tmux session changes (attached to
 * a different session, or the session was renamed). ``terminalId`` is the
 * per-tab id we pass into the ttyd URL when set (client-session-changed);
 * ``null`` for a rename, where the tab is matched on ``sessionId`` instead.
 */
export type TerminalSessionListener = (terminalId: string | null, sessionId: string, sessionName: string) => void;
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
let layoutSyncListeners: LayoutSyncListener[] = [];
let agentsUpdatedListeners: AgentsUpdatedListener[] = [];
let terminalSessionListeners: TerminalSessionListener[] = [];
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
    // Register this browser's identity + active layout with the server so
    // layout-targeted ops can find it. During startup the active layout may
    // not be chosen yet; DockviewWorkspace re-reports once it is.
    reportClientState();
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

    case "terminal_session":
      for (const listener of terminalSessionListeners) {
        listener(event.terminal_id, event.session_id, event.session_name);
      }
      break;

    case "layout_saved":
      for (const listener of layoutSyncListeners) {
        listener({
          kind: "saved",
          layoutSlug: event.layout_slug,
          displayName: event.display_name,
          savedByClientId: event.saved_by_client_id,
        });
      }
      break;

    case "layout_deleted":
      for (const listener of layoutSyncListeners) {
        listener({
          kind: "deleted",
          layoutSlug: event.layout_slug,
          fallbackLayoutSlug: event.fallback_layout_slug,
        });
      }
      break;

    case "load_layout":
      for (const listener of layoutSyncListeners) {
        listener({
          kind: "load",
          layoutSlug: event.layout_slug,
          displayName: event.display_name,
          targetClientId: event.target_client_id,
        });
      }
      break;
  }
}

/**
 * Report this browser's identity and active layout to the server over the
 * WebSocket (a `client_state` message). Called on WS open and whenever the
 * active layout changes; `previousLayoutSlug` is set on a switch so the
 * server can record a layout_switch event. No-op while the socket is down
 * or before an active layout has been chosen -- the next open re-reports.
 */
export function reportClientState(previousLayoutSlug?: string): void {
  const activeLayout = getActiveLayoutSlug();
  if (ws === null || ws.readyState !== WebSocket.OPEN || !activeLayout) {
    return;
  }
  ws.send(
    JSON.stringify({
      type: "client_state",
      client_id: getClientId(),
      active_layout: activeLayout,
      device_kind: getDeviceKind(),
      previous_layout: previousLayoutSlug ?? "",
    }),
  );
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

export function addLayoutSyncListener(listener: LayoutSyncListener): void {
  layoutSyncListeners.push(listener);
}

export function removeLayoutSyncListener(listener: LayoutSyncListener): void {
  layoutSyncListeners = layoutSyncListeners.filter((l) => l !== listener);
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

export function addTerminalSessionListener(listener: TerminalSessionListener): void {
  terminalSessionListeners.push(listener);
}

export function removeTerminalSessionListener(listener: TerminalSessionListener): void {
  terminalSessionListeners = terminalSessionListeners.filter((l) => l !== listener);
}

/** Fetch the live terminal-session fleet (all non-agent tmux sessions) plus
 *  the agent-session prefix. Defensive: returns an empty fleet with the
 *  default prefix if the request fails, so the "+" menu still renders. */
export async function fetchTerminalSessions(): Promise<{ terminals: TerminalSessionInfo[]; prefix: string }> {
  try {
    const response = await fetch(apiUrl("/api/terminals"));
    if (!response.ok) return { terminals: [], prefix: "mngr-" };
    const data = (await response.json()) as { terminals?: TerminalSessionInfo[]; prefix?: string };
    return { terminals: data.terminals ?? [], prefix: data.prefix ?? "mngr-" };
  } catch {
    return { terminals: [], prefix: "mngr-" };
  }
}

// The workspace terminal (ttyd) service is proxied at this same-origin path
// (the service dispatcher adds no base-path prefix). Kept here rather than in
// the view so the pure URL builder below is unit-testable without importing
// dockview-core (which needs a DOM).
const TERMINAL_SERVICE_URL_PATH = "/service/terminal/";

/** Build the ttyd URL that attaches a tab to a named tmux session via the
 *  ``session`` dispatch key. The ttyd dispatch reads the args positionally:
 *  ``$1`` ("_"), ``$2`` ("session"), ``$3`` (session name), ``$4`` (per-tab id
 *  used for live title tracking), ``$5`` (working dir for a fresh session;
 *  empty falls back to $HOME). ``new-session -A`` attaches if the session
 *  exists and creates it otherwise, which is what makes these terminals
 *  persistent in memory. */
export function buildSessionTerminalUrl(sessionName: string, terminalId: string, workdir: string): string {
  const params = new URLSearchParams();
  params.append("arg", "_");
  params.append("arg", "session");
  params.append("arg", sessionName);
  params.append("arg", terminalId);
  params.append("arg", workdir);
  return `${TERMINAL_SERVICE_URL_PATH}?${params.toString()}`;
}

/** Ask the backend to allocate the next free ``terminal-N`` session name. The
 *  backend inspects live tmux sessions and picks the lowest unused index under
 *  a lock, so concurrent "New terminal" clicks get distinct names. */
export async function allocateTerminalName(): Promise<string> {
  const response = await fetch(apiUrl("/api/terminals/allocate"), { method: "POST" });
  if (!response.ok) {
    throw new Error(`Failed to allocate terminal name (HTTP ${response.status})`);
  }
  const data = (await response.json()) as { session_name?: string };
  if (!data.session_name) {
    throw new Error("Terminal allocation returned no session_name");
  }
  return data.session_name;
}
