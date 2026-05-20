/**
 * Single shared dockview workspace. All agents, chats, terminals, and
 * applications coexist as tabs in one DockviewComponent.
 */

import m from "mithril";
import {
  DockviewComponent,
  themeLight,
  type IContentRenderer,
  type IHeaderActionsRenderer,
  type SerializedDockview,
} from "dockview-core";
import { ChatPanel } from "./ChatPanel";
import { IframePanel, IFRAME_PANEL_PANEL_ID_ATTR, reloadIframesForService } from "./IframePanel";
import { SubagentView } from "./SubagentView";
import { CreateAgentModal } from "./CreateAgentModal";
import { DestroyConfirmDialog } from "./DestroyConfirmDialog";
import { ShareModal } from "./ShareModal";
import { apiUrl, getPrimaryAgentId } from "../base-path";
import {
  addAgentsUpdatedListener,
  addLayoutOpListener,
  getAgentById,
  getAgents,
  getApplications,
  getProtoAgents,
  removeAgentLocally,
  type AgentsUpdatedListener,
  type LayoutOpEvent,
  type LayoutOpListener,
} from "../models/AgentManager";

const AUTOSAVE_DEBOUNCE_MS = 1500;

// SVG path constants for tab action icons
const SVG_CLOSE = '<line x1="4" y1="4" x2="12" y2="12"/><line x1="12" y1="4" x2="4" y2="12"/>';
const SVG_TRASH =
  '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>';
const SVG_SHARE =
  '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>';
const SVG_REFRESH =
  '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>';

// Every non-system_interface service is reached at /service/<name>/ on the
// same origin as the dockview UI itself. The workspace_server's service
// dispatcher handles the proxying, SW bootstrap, and header rewriting.
function getServiceUrl(serviceName: string): string {
  return `/service/${serviceName}/`;
}

export function getTerminalUrl(): string {
  return getServiceUrl("terminal");
}

type PanelType = "chat" | "iframe" | "subagent";

interface PanelParams {
  panelType: PanelType;
  agentId: string;
  chatAgentId?: string;
  url?: string;
  title?: string;
  subagentSessionId?: string;
  // Workspace service name this iframe is tied to (e.g. "web", "api").
  // Set only for iframe tabs that proxy an actual workspace service; left
  // undefined for ad-hoc URL tabs, terminals, and agent-owned iframes.
  // Drives both the WS-driven `layout_op` (op="refresh") service-wide
  // reload match and the presence of the per-tab Refresh button.
  serviceName?: string;
}

// Modal state
let showNewChatModal = false;
let showNewAgentModal = false;

// Destroy dialog state
let showDestroyDialog = false;
let destroyTargetAgentId: string | null = null;
let destroyTargetAgentName: string | null = null;
let destroyTargetPanelId: string | null = null;

// Share modal state
let showShareModal = false;
let shareServiceName: string | null = null;

interface SavedLayout {
  dockview: SerializedDockview;
  panelParams: Record<string, PanelParams>;
}

// Single shared dockview state
let dockview: DockviewComponent | null = null;
let dockviewContainer: HTMLElement | null = null;
const panelParams = new Map<string, PanelParams>();
let saveTimer: ReturnType<typeof setTimeout> | null = null;
let _layoutOpListener: LayoutOpListener | null = null;
let initialized = false;

// Target fraction of horizontal space that the newly-opened service panel
// takes when it splits alongside the primary agent's chat. Picked so the
// just-built view dominates while the chat stays legible.
const OPEN_TAB_SPLIT_FRACTION = 0.6;

function createMithrilRenderer(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  component: m.ComponentTypes<any, any>,
  attrs: Record<string, unknown>,
): IContentRenderer {
  const element = document.createElement("div");
  element.style.width = "100%";
  element.style.height = "100%";
  element.style.display = "flex";
  element.style.flexDirection = "column";

  return {
    element,
    init() {
      m.mount(element, { view: () => m(component, attrs) });
    },
    dispose() {
      m.mount(element, null);
    },
  };
}

function makeSvgIcon(pathContent: string, viewBox: string = "0 0 24 24"): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${viewBox}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${pathContent}</svg>`;
}

function createTabActionButton(
  title: string,
  svgPath: string,
  onClick: (ev: MouseEvent) => void,
  className: string = "",
): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.className = `dv-custom-tab-action ${className}`.trim();
  btn.title = title;
  btn.innerHTML = makeSvgIcon(svgPath);
  btn.addEventListener("pointerdown", (ev) => ev.preventDefault());
  btn.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    onClick(ev);
  });
  return btn;
}

function createCustomTab(options: { id: string; name: string }): {
  element: HTMLElement;
  init: (params: {
    title: string;
    api: {
      close: () => void;
      onDidTitleChange: (cb: (e: { title: string }) => void) => { dispose: () => void };
      isActive: boolean;
      onDidActiveChange: (cb: (e: { isActive: boolean }) => void) => { dispose: () => void };
    };
  }) => void;
  dispose: () => void;
} {
  const element = document.createElement("div");
  element.className = "dv-default-tab dv-custom-tab";

  const content = document.createElement("div");
  content.className = "dv-default-tab-content";
  element.appendChild(content);

  const actions = document.createElement("div");
  actions.className = "dv-custom-tab-actions";
  actions.style.display = "none";
  element.appendChild(actions);

  const disposables: Array<{ dispose: () => void }> = [];

  return {
    element,
    init(params) {
      content.textContent = params.title ?? "";
      disposables.push(
        params.api.onDidTitleChange((event) => {
          content.textContent = event.title ?? "";
        }),
      );

      const pp = panelParams.get(options.id);
      const panelType = pp?.panelType ?? "chat";

      // Share and Refresh buttons -- only on iframe/application tabs.
      // The Refresh button matches open iframes by their data-service-name
      // attribute, which is populated only when the tab is tied to a real
      // workspace service. For tabs without an explicit serviceName
      // (terminals, custom URLs, agent-owned iframes), suppress the Refresh
      // button since there is nothing to match against.
      if (panelType === "iframe") {
        const shareName = pp?.serviceName ?? pp?.title ?? "web";
        if (pp?.serviceName) {
          const serviceName = pp.serviceName;
          actions.appendChild(
            createTabActionButton("Refresh", SVG_REFRESH, () => {
              reloadIframesForService(serviceName);
            }),
          );
        }
        actions.appendChild(
          createTabActionButton("Share", SVG_SHARE, () => {
            shareServiceName = shareName;
            showShareModal = true;
            m.redraw();
          }),
        );
      }

      // Destroy button -- on chat/agent tabs (except the primary agent)
      if (panelType === "chat") {
        const chatAgentId = pp?.chatAgentId ?? pp?.agentId ?? "";
        const primaryAgentId = getPrimaryAgentId();
        const isPrimary = chatAgentId === primaryAgentId;

        const destroyBtn = createTabActionButton(
          isPrimary ? "Cannot destroy the primary agent" : "Destroy agent",
          SVG_TRASH,
          () => {
            if (isPrimary) return;
            const agent = getAgentById(chatAgentId);
            destroyTargetAgentId = chatAgentId;
            destroyTargetAgentName = agent?.name ?? chatAgentId;
            destroyTargetPanelId = options.id;
            showDestroyDialog = true;
            m.redraw();
          },
          isPrimary ? "dv-custom-tab-action-disabled" : "dv-custom-tab-action-destructive",
        );
        if (isPrimary) {
          destroyBtn.disabled = true;
        }
        actions.appendChild(destroyBtn);
      }

      // Close button -- on all tab types
      actions.appendChild(
        createTabActionButton("Close tab", SVG_CLOSE, () => {
          params.api.close();
        }),
      );

      // Show/hide actions based on active state
      function updateActionsVisibility(isActive: boolean): void {
        actions.style.display = isActive ? "flex" : "none";
      }
      updateActionsVisibility(params.api.isActive);
      disposables.push(
        params.api.onDidActiveChange((event) => {
          updateActionsVisibility(event.isActive);
        }),
      );
    },
    dispose() {
      for (const d of disposables) {
        d.dispose();
      }
      disposables.length = 0;
    },
  };
}

/** Get the set of agent IDs that currently have open chat panels. */
function getOpenChatAgentIds(): Set<string> {
  const ids = new Set<string>();
  for (const [, pp] of panelParams) {
    if (pp.panelType === "chat") {
      ids.add(pp.chatAgentId ?? pp.agentId);
    }
  }
  return ids;
}

/** Get the set of application names that currently have open iframe panels. */
function getOpenAppNames(): Set<string> {
  const names = new Set<string>();
  for (const [, pp] of panelParams) {
    if (pp.panelType === "iframe" && pp.title) {
      names.add(pp.title);
    }
  }
  return names;
}

function buildDropdownItems(): Array<{ label: string; action: () => void; dividerAfter?: boolean }> {
  const items: Array<{ label: string; action: () => void; dividerAfter?: boolean }> = [];
  const openChatIds = getOpenChatAgentIds();
  const openAppNames = getOpenAppNames();
  const primaryId = getPrimaryAgentId();

  // --- Existing items section ---

  // Applications that don't have open tabs. Exclude "system_interface"
  // (that's the surrounding chrome UI, not a tab-able app) and "terminal"
  // (reachable via the "New terminal" menu item further down). Everything
  // else, including the default "web" example server, is openable.
  const apps = getApplications().filter((app) => app.name !== "system_interface" && app.name !== "terminal");
  for (const app of apps) {
    if (!openAppNames.has(app.name)) {
      const proxyUrl = getServiceUrl(app.name);
      items.push({
        label: app.name,
        action: () => openIframeTab(proxyUrl, app.name, "iframe", app.name),
      });
    }
  }

  // Agents/chats that don't have open tabs
  const allAgents = getAgents();
  for (const agent of allAgents) {
    if (!openChatIds.has(agent.id)) {
      items.push({
        label: agent.name,
        action: () => addChatPanel(agent.id, agent.name),
      });
    }
  }

  // Proto-agents that don't have open tabs
  const protos = getProtoAgents();
  for (const proto of protos) {
    if (!openChatIds.has(proto.agent_id)) {
      items.push({
        label: `${proto.name} (creating...)`,
        action: () => addChatPanel(proto.agent_id, proto.name),
      });
    }
  }

  // Add divider if we had existing items
  if (items.length > 0) {
    items[items.length - 1].dividerAfter = true;
  }

  // --- "New ..." items ---

  items.push({
    label: "New chat",
    action: () => {
      showNewChatModal = true;
      m.redraw();
    },
  });

  // Terminal -- always primary agent's work_dir
  const primaryAgent = getAgentById(primaryId);
  const terminalBaseUrl = getTerminalUrl();
  const terminalUrl = primaryAgent?.work_dir
    ? `${terminalBaseUrl}?arg=_&arg=workdir&arg=${encodeURIComponent(primaryAgent.work_dir)}`
    : terminalBaseUrl;
  items.push({
    label: "New terminal",
    action: () => openIframeTab(terminalUrl, "terminal"),
  });

  items.push({
    label: "New URL",
    action: () => showCustomUrlDialog(),
  });

  items.push({
    label: "New agent",
    action: () => {
      showNewAgentModal = true;
      m.redraw();
    },
  });

  return items;
}

function createAddTabButton(): IHeaderActionsRenderer {
  const element = document.createElement("div");
  element.className = "dockview-add-tab-wrapper";

  const button = document.createElement("button");
  button.className = "dockview-add-tab-button";
  button.title = "Add tab";
  button.textContent = "+";
  element.appendChild(button);

  const dropdown = document.createElement("div");
  dropdown.className = "dockview-add-tab-dropdown";
  dropdown.style.display = "none";

  element.appendChild(dropdown);

  button.addEventListener("click", (e) => {
    e.stopPropagation();
    const isVisible = dropdown.style.display !== "none";
    if (isVisible) {
      dropdown.style.display = "none";
    } else {
      dropdown.innerHTML = "";
      const items = buildDropdownItems();
      for (const item of items) {
        const menuItem = document.createElement("div");
        menuItem.className = "dockview-add-tab-dropdown-item";
        menuItem.textContent = item.label;
        menuItem.addEventListener("click", (clickEvent) => {
          clickEvent.stopPropagation();
          dropdown.style.display = "none";
          item.action();
        });
        dropdown.appendChild(menuItem);

        if (item.dividerAfter) {
          const divider = document.createElement("div");
          divider.className = "dockview-add-tab-dropdown-divider";
          divider.style.borderTop = "1px solid #e5e7eb";
          divider.style.margin = "4px 0";
          dropdown.appendChild(divider);
        }
      }
      dropdown.style.display = "block";
    }
  });

  const closeDropdown = (e: MouseEvent) => {
    if (!element.contains(e.target as Node)) {
      dropdown.style.display = "none";
    }
  };
  document.addEventListener("click", closeDropdown);

  return {
    element,
    init() {},
    dispose() {
      document.removeEventListener("click", closeDropdown);
    },
  };
}

function focusOrCreateChatPanel(chatAgentId: string, chatAgentName: string): void {
  if (!dockview) return;
  const panelId = `chat-${chatAgentId}`;
  const existingPanel = dockview.panels.find((p) => p.id === panelId);
  if (existingPanel) {
    if (!existingPanel.api.isActive) {
      dockview.setActivePanel(existingPanel);
    }
    return;
  }
  addChatPanel(chatAgentId, chatAgentName);
}

function addChatPanel(chatAgentId: string, chatAgentName: string): void {
  if (!dockview) return;
  const panelId = `chat-${chatAgentId}`;
  const params: PanelParams = { panelType: "chat", agentId: chatAgentId, chatAgentId };
  panelParams.set(panelId, params);
  dockview.addPanel({
    id: panelId,
    component: "chat",
    title: chatAgentName,
    params,
    renderer: "always",
  });
}

/**
 * Open the workspace's initial (bootstrap-created) chat agent as the first
 * tab. "Initial" = the earliest non-is_primary agent we know about. In a
 * freshly-booted workspace the bootstrap creates exactly one chat agent
 * named after the host, and that's what we want here. The services agent
 * (is_primary=true) is filtered out by getAgents().
 *
 * If no non-is_primary agent exists yet (e.g. the workspace just started
 * and the bootstrap's `mngr create` is still running), returns false so
 * the caller can show a "waiting" state. We re-try when an agents_updated
 * event arrives.
 */
function openInitialChatTab(): boolean {
  const candidates = getAgents();
  if (candidates.length === 0) return false;
  const initial = candidates[0];
  addChatPanel(initial.id, initial.name);
  return true;
}

// `awaitingInitialChat` flips on when init runs against an empty agent
// list, and back off as soon as we successfully open the initial tab.
// While true, an agents_updated listener keeps retrying. The empty-state
// overlay uses this flag to decide whether to show "Waiting for initial
// chat agent..." vs the default "+ Open new tab" message.
let awaitingInitialChat = false;
let agentsUpdatedListener: AgentsUpdatedListener | null = null;

function openIframeTab(url: string, title: string, panelType: PanelType = "iframe", serviceName?: string): void {
  if (!dockview) return;
  const primaryId = getPrimaryAgentId();
  const panelId = `${panelType}-${primaryId}-${Date.now()}`;
  const params: PanelParams = { panelType, agentId: primaryId, url, title, serviceName };
  panelParams.set(panelId, params);
  dockview.addPanel({
    id: panelId,
    component: "iframe",
    title,
    params,
  });
}

/** Find the chat panel id to anchor an agent-initiated split against.
 *
 *  Strict identity: the only acceptable anchor is the requester's own chat
 *  tab (``chat-<requesterAgentId>``). Returns null when the requester id is
 *  empty or their chat panel isn't open -- callers then either fall through
 *  to a non-chat-anchored placement (``handleOpenTabRequest``) or no-op
 *  (``handleSplit`` / ``handleMove`` skip the relative_to=self branch). We
 *  intentionally do not auto-select another agent's chat: that would let
 *  ``layout.py split web`` from agent A land next to agent B's chat
 *  whenever A's chat happens not to be on screen, which is surprising. */
function findAnchorChatPanelId(requesterAgentId: string): string | null {
  if (!dockview) return null;
  if (!requesterAgentId) return null;
  const candidate = `chat-${requesterAgentId}`;
  return dockview.panels.find((p) => p.id === candidate) ? candidate : null;
}

/** Find an existing iframe panel for ``serviceName``, or null. */
function findIframePanelIdForService(serviceName: string): string | null {
  for (const [panelId, params] of panelParams) {
    if (params.panelType === "iframe" && params.serviceName === serviceName) {
      return panelId;
    }
  }
  return null;
}

/** Find a group adjacent to ``anchorGroupId`` in the requested direction.
 *
 *  Used by the "share existing splits" default for ``open`` / ``split`` /
 *  ``move``: if the caller asked to put a panel to the right of (say) a
 *  chat, and a service iframe is already living to the right of that
 *  chat, we'd rather tab the new panel into that existing group than
 *  jam another column between them.
 *
 *  Adjacency is measured geometrically off ``getBoundingClientRect`` --
 *  walking the persisted grid tree would also work but ties us to
 *  dockview-internal APIs that aren't part of its public surface.
 *  Among multiple candidates we pick the one with the largest overlap
 *  on the perpendicular axis: e.g. for ``direction: "right"`` we prefer
 *  the group whose vertical extent most closely tracks the anchor's.
 *  Returns null when no group lies in that direction. */
function findSiblingGroupInDirection(
  anchorGroupId: string,
  direction: "left" | "right" | "above" | "below",
): { id: string } | null {
  if (!dockview) return null;
  const anchor = dockview.groups.find((g) => g.id === anchorGroupId);
  if (!anchor) return null;
  const anchorRect = anchor.element.getBoundingClientRect();
  // Pixel slop: dockview separators round to whole pixels and adjacent
  // edges can be off-by-one after a resize.
  const tolerance = 2;
  let best: { id: string; overlap: number; distance: number } | null = null;
  for (const group of dockview.groups) {
    if (group.id === anchorGroupId) continue;
    const rect = group.element.getBoundingClientRect();
    let inDirection: boolean;
    let overlap: number;
    let distance: number;
    if (direction === "right") {
      inDirection = rect.left >= anchorRect.right - tolerance;
      overlap = Math.max(0, Math.min(rect.bottom, anchorRect.bottom) - Math.max(rect.top, anchorRect.top));
      distance = rect.left - anchorRect.right;
    } else if (direction === "left") {
      inDirection = rect.right <= anchorRect.left + tolerance;
      overlap = Math.max(0, Math.min(rect.bottom, anchorRect.bottom) - Math.max(rect.top, anchorRect.top));
      distance = anchorRect.left - rect.right;
    } else if (direction === "below") {
      inDirection = rect.top >= anchorRect.bottom - tolerance;
      overlap = Math.max(0, Math.min(rect.right, anchorRect.right) - Math.max(rect.left, anchorRect.left));
      distance = rect.top - anchorRect.bottom;
    } else {
      inDirection = rect.bottom <= anchorRect.top + tolerance;
      overlap = Math.max(0, Math.min(rect.right, anchorRect.right) - Math.max(rect.left, anchorRect.left));
      distance = anchorRect.top - rect.bottom;
    }
    if (!inDirection || overlap <= 0) continue;
    // Prefer larger perpendicular overlap; break ties by nearer distance.
    if (best === null || overlap > best.overlap || (overlap === best.overlap && distance < best.distance)) {
      best = { id: group.id, overlap, distance };
    }
  }
  return best === null ? null : { id: best.id };
}

/** Handle an agent-driven open_tab broadcast for ``serviceName``.
 *
 *  Resolution order:
 *    1. If a panel for ``serviceName`` is already open, focus it.
 *    2. If the *requester's own* chat panel is open, add a right-split
 *       iframe sized to ``OPEN_TAB_SPLIT_FRACTION`` of the dockview
 *       container width, anchored on that chat. The previous broader
 *       fallback (primary's chat, then any open chat) was dropped to
 *       avoid landing one agent's service next to a different agent's
 *       chat just because the requester's chat happened to be closed.
 *    3. Otherwise, add a plain iframe tab with dockview's default placement.
 *  Drop silently if the service isn't registered in ``applications`` yet --
 *  the script polls registration, but the WS broadcast itself is fire-and-
 *  forget. */
function handleOpenTabRequest(serviceName: string, requesterAgentId: string, forceNewGroup: boolean): void {
  if (!dockview) return;

  const existingPanelId = findIframePanelIdForService(serviceName);
  if (existingPanelId !== null) {
    const existing = dockview.panels.find((p) => p.id === existingPanelId);
    if (existing) {
      dockview.setActivePanel(existing);
    }
    return;
  }

  const app = getApplications().find((a) => a.name === serviceName);
  if (!app) return;

  const ownerId = requesterAgentId || getPrimaryAgentId();
  const panelId = `iframe-${ownerId}-${Date.now()}`;
  const params: PanelParams = {
    panelType: "iframe",
    agentId: ownerId,
    url: getServiceUrl(serviceName),
    title: serviceName,
    serviceName,
  };
  panelParams.set(panelId, params);

  const chatPanelId = findAnchorChatPanelId(requesterAgentId);
  if (chatPanelId !== null) {
    // Default: tab into an existing group to the right of the anchor
    // chat if one is open. Callers pass ``forceNewGroup`` to demand a
    // fresh column instead. See ``findSiblingGroupInDirection`` for the
    // adjacency rule.
    const anchorPanel = dockview.panels.find((p) => p.id === chatPanelId);
    const anchorGroupId = anchorPanel?.api.group.id ?? null;
    const sibling =
      !forceNewGroup && anchorGroupId !== null ? findSiblingGroupInDirection(anchorGroupId, "right") : null;
    if (sibling !== null) {
      dockview.addPanel({
        id: panelId,
        component: "iframe",
        title: serviceName,
        params,
        position: { referenceGroup: sibling.id },
      });
      return;
    }
    const containerWidth = dockviewContainer?.getBoundingClientRect().width ?? 0;
    const initialWidth = containerWidth > 0 ? Math.round(containerWidth * OPEN_TAB_SPLIT_FRACTION) : undefined;
    dockview.addPanel({
      id: panelId,
      component: "iframe",
      title: serviceName,
      params,
      position: { referencePanel: chatPanelId, direction: "right" },
      initialWidth,
    });
    return;
  }

  dockview.addPanel({
    id: panelId,
    component: "iframe",
    title: serviceName,
    params,
  });
}

export function openIframeTabForAgent(_agentId: string, url: string, title: string): void {
  openIframeTab(url, title);
}

export function openSubagentTab(agentId: string, subagentSessionId: string, description: string): void {
  if (!dockview) return;

  const existingPanel = dockview.panels.find((p) => {
    const params = panelParams.get(p.id);
    return params?.panelType === "subagent" && params.subagentSessionId === subagentSessionId;
  });
  if (existingPanel) {
    dockview.setActivePanel(existingPanel);
    return;
  }

  const panelId = `subagent-${agentId}-${subagentSessionId}`;
  const params: PanelParams = {
    panelType: "subagent",
    agentId,
    subagentSessionId,
    title: description,
  };
  panelParams.set(panelId, params);
  dockview.addPanel({
    id: panelId,
    component: "subagent",
    title: description,
    params,
  });
}

function showCustomUrlDialog(): void {
  const overlay = document.createElement("div");
  overlay.className = "custom-url-dialog-overlay";

  const dialog = document.createElement("div");
  dialog.className = "custom-url-dialog";

  dialog.innerHTML = `
    <h3 class="custom-url-dialog-title">Open Custom URL</h3>
    <label class="custom-url-dialog-label">URL</label>
    <input type="url" class="custom-url-dialog-input" placeholder="https://example.com" autofocus />
    <label class="custom-url-dialog-label">Title (optional)</label>
    <input type="text" class="custom-url-dialog-input" placeholder="Tab title" />
    <div class="custom-url-dialog-actions">
      <button class="custom-url-dialog-cancel">Cancel</button>
      <button class="custom-url-dialog-open">Open</button>
    </div>
  `;

  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  const inputs = dialog.querySelectorAll("input");
  const urlInput = inputs[0] as HTMLInputElement;
  const titleInput = inputs[1] as HTMLInputElement;

  function close(): void {
    document.body.removeChild(overlay);
  }

  function open(): void {
    const url = urlInput.value.trim();
    if (!url) return;

    let title = titleInput.value.trim();
    if (!title) {
      try {
        title = new URL(url).hostname;
      } catch {
        title = url;
      }
    }
    close();
    openIframeTab(url, title);
  }

  dialog.querySelector(".custom-url-dialog-cancel")!.addEventListener("click", close);
  dialog.querySelector(".custom-url-dialog-open")!.addEventListener("click", open);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });
  urlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") open();
    if (e.key === "Escape") close();
  });
  titleInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") open();
    if (e.key === "Escape") close();
  });

  urlInput.focus();
}

async function saveLayout(): Promise<void> {
  if (!dockview) return;

  const dockviewJson = dockview.toJSON();
  const serializedParams: Record<string, PanelParams> = {};
  for (const [id, params] of panelParams) {
    serializedParams[id] = params;
  }
  const payload: SavedLayout = { dockview: dockviewJson, panelParams: serializedParams };

  try {
    await fetch(apiUrl(`/api/layout`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch {
    // Layout save is best-effort
  }
}

function scheduleSave(): void {
  if (saveTimer !== null) {
    clearTimeout(saveTimer);
  }
  saveTimer = setTimeout(() => {
    saveTimer = null;
    saveLayout();
  }, AUTOSAVE_DEBOUNCE_MS);
}

async function loadLayout(): Promise<SavedLayout | null> {
  try {
    const response = await fetch(apiUrl(`/api/layout`));
    if (!response.ok) return null;
    return (await response.json()) as SavedLayout;
  } catch {
    return null;
  }
}

// ---------- Agent-driven layout op handlers ----------

/** First eight hex chars of the panel id's SHA-256, matching the
 *  server-side ``_short_hash`` used to build ``terminal:`` / ``url:`` refs. */
async function shortHash(panelId: string): Promise<string> {
  const data = new TextEncoder().encode(panelId);
  const buffer = await crypto.subtle.digest("SHA-256", data);
  const bytes = new Uint8Array(buffer);
  let hex = "";
  for (let i = 0; i < 4; i++) {
    hex += bytes[i].toString(16).padStart(2, "0");
  }
  return hex;
}

/** Map a ``direction`` from the layout op (left/right/above/below) onto
 *  dockview's ``Position`` enum used by ``panel.api.moveTo``. */
function directionToPosition(direction: string): "top" | "bottom" | "left" | "right" {
  switch (direction) {
    case "above":
      return "top";
    case "below":
      return "bottom";
    case "left":
      return "left";
    case "right":
      return "right";
    default:
      return "right";
  }
}

/** Resolve a layout-op ref (or the literal "self") to a live dockview
 *  panel id. Returns null when no matching panel is currently open --
 *  callers decide whether that's fatal (close/focus) or a no-op cue to
 *  fall back to a creation path (open/split). */
async function resolveRefToPanelId(ref: string, requesterAgentId: string): Promise<string | null> {
  if (!dockview) return null;
  if (ref === "self") {
    // ``self`` is the *identity* ref for the caller's own chat panel
    // (``chat-<requesterAgentId>``). Returns null when the requester
    // didn't set ``MNGR_AGENT_ID`` or when their chat tab isn't open.
    // All layout ops (including ``relative_to=self`` on split/move)
    // honor this strict identity to avoid silently retargeting another
    // agent's chat.
    if (!requesterAgentId) return null;
    const candidate = `chat-${requesterAgentId}`;
    return dockview.panels.find((p) => p.id === candidate) ? candidate : null;
  }
  if (ref.startsWith("service:")) {
    return findIframePanelIdForService(ref.substring("service:".length));
  }
  if (ref.startsWith("chat:")) {
    const agentName = ref.substring("chat:".length);
    const agent = getAgents().find((a) => a.name === agentName);
    if (!agent) return null;
    const candidate = `chat-${agent.id}`;
    return dockview.panels.find((p) => p.id === candidate) ? candidate : null;
  }
  if (ref.startsWith("subagent:")) {
    const sessionId = ref.substring("subagent:".length);
    for (const [panelId, p] of panelParams) {
      if (p.panelType === "subagent" && p.subagentSessionId === sessionId) return panelId;
    }
    return null;
  }
  if (ref.startsWith("url:") || ref.startsWith("terminal:")) {
    const hash = ref.split(":")[1] ?? "";
    for (const panelId of panelParams.keys()) {
      const candidateHash = await shortHash(panelId);
      if (candidateHash === hash) return panelId;
    }
    return null;
  }
  return null;
}

/** Resolve a ``service:<name>[/<path>]`` shorthand URL (sent by
 *  ``replace-url``) to the on-origin ``/service/<name>/<path>`` path that
 *  the dispatcher serves. Plain ``https://`` URLs pass through. */
function resolveReplaceUrl(url: string): string {
  if (url.startsWith("service:")) {
    const remainder = url.substring("service:".length);
    const slashIndex = remainder.indexOf("/");
    if (slashIndex === -1) return getServiceUrl(remainder);
    const serviceName = remainder.substring(0, slashIndex);
    const path = remainder.substring(slashIndex + 1);
    return `/service/${serviceName}/${path}`;
  }
  return url;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

async function handleLayoutOp(event: LayoutOpEvent): Promise<void> {
  if (!dockview) return;
  const requesterAgentId = event.requesterAgentId;
  switch (event.op) {
    case "open":
      await handleOpen(event.args, requesterAgentId);
      return;
    case "focus":
      await handleFocus(event.args, requesterAgentId);
      return;
    case "split":
      await handleSplit(event.args, requesterAgentId);
      return;
    case "close":
      await handleClose(event.args, requesterAgentId);
      return;
    case "move":
      await handleMove(event.args, requesterAgentId);
      return;
    case "rename":
      await handleRename(event.args, requesterAgentId);
      return;
    case "maximize":
      await handleMaximize(event.args, requesterAgentId);
      return;
    case "restore":
      handleRestore();
      return;
    case "replace-url":
      await handleReplaceUrl(event.args, requesterAgentId);
      return;
    case "refresh":
      await handleRefresh(event.args, requesterAgentId);
      return;
  }
}

async function handleOpen(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  const ref = asString(args.ref);
  if (!ref || !dockview) return;
  const existing = await resolveRefToPanelId(ref, requesterAgentId);
  if (existing !== null) {
    const panel = dockview.panels.find((p) => p.id === existing);
    if (panel) dockview.setActivePanel(panel);
    return;
  }
  if (ref.startsWith("service:")) {
    const forceNewGroup = args.new_group === true;
    handleOpenTabRequest(ref.substring("service:".length), requesterAgentId, forceNewGroup);
    return;
  }
  if (ref.startsWith("chat:")) {
    const agentName = ref.substring("chat:".length);
    const agent = getAgents().find((a) => a.name === agentName);
    if (agent) addChatPanel(agent.id, agent.name);
    return;
  }
  // Other ref kinds (subagent/terminal/url) are not creatable from an
  // ``open`` op: their stable refs only exist after creation through
  // the surrounding code paths (e.g. SubagentView, "New URL" dialog).
}

async function handleFocus(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const ref = asString(args.ref);
  if (!ref) return;
  const panelId = await resolveRefToPanelId(ref, requesterAgentId);
  if (panelId === null) return;
  const panel = dockview.panels.find((p) => p.id === panelId);
  if (panel) dockview.setActivePanel(panel);
}

async function handleSplit(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const ref = asString(args.ref);
  const relativeTo = asString(args.relative_to);
  const direction = asString(args.direction) ?? "right";
  const ratio = asNumber(args.ratio);
  const forceNewGroup = args.new_group === true;
  if (!ref || !relativeTo) return;

  // ``relative_to=self`` strictly anchors against the requester's own chat
  // panel. If their chat isn't open (or they didn't set ``MNGR_AGENT_ID``),
  // the op is a no-op rather than landing next to some other agent's chat.
  const referencePanelId = await resolveRefToPanelId(relativeTo, requesterAgentId);
  if (referencePanelId === null) return;

  if (!ref.startsWith("service:") && !ref.startsWith("chat:")) {
    // ``split`` only creates new service/chat panels in v1; terminal,
    // subagent, and ad-hoc URL panels are created through other UI
    // paths and addressed by short-hash refs once they exist.
    return;
  }

  const containerRect = dockviewContainer?.getBoundingClientRect();
  const sizes = computeInitialSize(direction, ratio, containerRect);

  // Default: when a group already lives in the requested direction
  // relative to the anchor, tab the new panel into that group instead
  // of carving a new column. ``new_group`` opts back in to the
  // always-fresh-column behavior.
  const directionArg = directionFromArg(direction);
  const referencePanel = dockview.panels.find((p) => p.id === referencePanelId);
  const anchorGroupId = referencePanel?.api.group.id ?? null;
  const sibling =
    !forceNewGroup && anchorGroupId !== null ? findSiblingGroupInDirection(anchorGroupId, directionArg) : null;
  const positionOptions =
    sibling !== null ? { referenceGroup: sibling.id } : { referencePanel: referencePanelId, direction: directionArg };
  // Size hints only apply when we're carving a new group; tabbing into
  // an existing group ignores them anyway, so omit to keep intent clear.
  const sizeOptions = sibling !== null ? {} : sizes;

  if (ref.startsWith("service:")) {
    const serviceName = ref.substring("service:".length);
    // ``service:<name>`` uniquely identifies a panel via panelParams, so
    // splitting a service that's already open would leave a duplicate
    // addressable only by short-hash. Focus the existing panel instead,
    // matching the chat-ref branch below and the dedup ``open`` does.
    const existingServicePanelId = findIframePanelIdForService(serviceName);
    if (existingServicePanelId !== null) {
      const existing = dockview.panels.find((p) => p.id === existingServicePanelId);
      if (existing) dockview.setActivePanel(existing);
      return;
    }
    const ownerId = requesterAgentId || getPrimaryAgentId();
    const panelId = `iframe-${ownerId}-${Date.now()}`;
    const params: PanelParams = {
      panelType: "iframe",
      agentId: ownerId,
      url: getServiceUrl(serviceName),
      title: serviceName,
      serviceName,
    };
    panelParams.set(panelId, params);
    dockview.addPanel({
      id: panelId,
      component: "iframe",
      title: serviceName,
      params,
      position: positionOptions,
      ...sizeOptions,
    });
    return;
  }
  // ref starts with "chat:"
  const agentName = ref.substring("chat:".length);
  const agent = getAgents().find((a) => a.name === agentName);
  if (!agent) return;
  const panelId = `chat-${agent.id}`;
  if (dockview.panels.some((p) => p.id === panelId)) return;
  const params: PanelParams = { panelType: "chat", agentId: agent.id, chatAgentId: agent.id };
  panelParams.set(panelId, params);
  dockview.addPanel({
    id: panelId,
    component: "chat",
    title: agent.name,
    params,
    renderer: "always",
    position: positionOptions,
    ...sizeOptions,
  });
}

function directionFromArg(direction: string): "left" | "right" | "above" | "below" {
  if (direction === "left" || direction === "right" || direction === "above" || direction === "below") {
    return direction;
  }
  return "right";
}

function computeInitialSize(
  direction: string,
  ratio: number | null,
  containerRect: DOMRect | undefined,
): { initialWidth?: number; initialHeight?: number } {
  if (ratio === null || !containerRect) return {};
  if (direction === "above" || direction === "below") {
    const h = containerRect.height > 0 ? Math.round(containerRect.height * ratio) : undefined;
    return h ? { initialHeight: h } : {};
  }
  const w = containerRect.width > 0 ? Math.round(containerRect.width * ratio) : undefined;
  return w ? { initialWidth: w } : {};
}

async function handleClose(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const ref = asString(args.ref);
  if (!ref) return;
  const panelId = await resolveRefToPanelId(ref, requesterAgentId);
  if (panelId === null) return;
  const panel = dockview.panels.find((p) => p.id === panelId);
  if (panel) dockview.removePanel(panel);
}

async function handleMove(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const ref = asString(args.ref);
  const relativeTo = asString(args.relative_to);
  const direction = asString(args.direction);
  const forceNewGroup = args.new_group === true;
  if (!ref || !relativeTo || !direction) return;
  const targetPanelId = await resolveRefToPanelId(ref, requesterAgentId);
  // ``relative_to`` follows the same strict-identity rule as ``handleSplit``:
  // ``self`` resolves to the requester's chat or nothing.
  const referencePanelId = await resolveRefToPanelId(relativeTo, requesterAgentId);
  if (targetPanelId === null || referencePanelId === null) return;
  const targetPanel = dockview.panels.find((p) => p.id === targetPanelId);
  const referencePanel = dockview.panels.find((p) => p.id === referencePanelId);
  if (!targetPanel || !referencePanel) return;
  // Same share-group default as handleSplit: if a group already lives
  // in the requested direction, drop the panel into it as a tab unless
  // the caller asked for a brand-new group.
  const directionArg = directionFromArg(direction);
  const anchorGroupId = referencePanel.api.group.id;
  const sibling = !forceNewGroup ? findSiblingGroupInDirection(anchorGroupId, directionArg) : null;
  if (sibling !== null) {
    const siblingGroup = dockview.groups.find((g) => g.id === sibling.id);
    if (siblingGroup) {
      targetPanel.api.moveTo({ group: siblingGroup });
      return;
    }
  }
  targetPanel.api.moveTo({
    group: referencePanel.api.group,
    position: directionToPosition(direction),
  });
}

async function handleRename(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const ref = asString(args.ref);
  const title = asString(args.title);
  if (!ref || !title) return;
  const panelId = await resolveRefToPanelId(ref, requesterAgentId);
  if (panelId === null) return;
  const panel = dockview.panels.find((p) => p.id === panelId);
  if (!panel) return;
  panel.api.setTitle(title);
  const params = panelParams.get(panelId);
  if (params) {
    params.title = title;
  }
}

async function handleMaximize(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const ref = asString(args.ref);
  if (!ref) return;
  const panelId = await resolveRefToPanelId(ref, requesterAgentId);
  if (panelId === null) return;
  const panel = dockview.panels.find((p) => p.id === panelId);
  if (panel) panel.api.maximize();
}

function handleRestore(): void {
  if (!dockview) return;
  for (const panel of dockview.panels) {
    if (panel.api.isMaximized()) {
      panel.api.exitMaximized();
      return;
    }
  }
}

async function handleReplaceUrl(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  const ref = asString(args.ref);
  const url = asString(args.url);
  if (!ref || !url) return;
  const panelId = await resolveRefToPanelId(ref, requesterAgentId);
  if (panelId === null) return;
  const params = panelParams.get(panelId);
  if (!params || params.panelType !== "iframe") return;
  params.url = resolveReplaceUrl(url);
  m.redraw();
  scheduleSave();
}

async function handleRefresh(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  const ref = asString(args.ref);
  if (!ref) return;
  if (ref.startsWith("service:")) {
    reloadIframesForService(ref.substring("service:".length));
    return;
  }
  const panelId = await resolveRefToPanelId(ref, requesterAgentId);
  if (panelId === null) return;
  const params = panelParams.get(panelId);
  if (!params || params.panelType !== "iframe") return;
  // Single-panel reload: look the iframe up by its panel-id attribute
  // and trigger a same-origin ``contentWindow.location.reload()``. If the
  // panel is cross-origin the ``reload()`` call throws a SecurityError and
  // we fall back to re-assigning ``src`` to force the browser to refetch.
  const iframe = document.querySelector<HTMLIFrameElement>(
    `iframe[${IFRAME_PANEL_PANEL_ID_ATTR}="${CSS.escape(panelId)}"]`,
  );
  if (iframe) {
    try {
      const win = iframe.contentWindow;
      if (win !== null) {
        win.location.reload();
        return;
      }
    } catch {
      // Cross-origin: fall through to src reassignment.
    }
    const currentSrc = iframe.getAttribute("src");
    if (currentSrc !== null) iframe.setAttribute("src", currentSrc);
  }
}

/** Build a dockview content renderer for an iframe panel that re-reads
 *  ``panelParams[panelId]`` on every mithril redraw. This keeps the
 *  iframe in sync with agent-driven mutations to ``url``/``title`` so
 *  ``replace-url`` doesn't need to remove-and-recreate the panel. */
function createReactiveIframeRenderer(panelId: string): IContentRenderer {
  const element = document.createElement("div");
  element.style.width = "100%";
  element.style.height = "100%";
  element.style.display = "flex";
  element.style.flexDirection = "column";
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const iframePanelComponent: m.ComponentTypes<any, any> = IframePanel;
  return {
    element,
    init() {
      m.mount(element, {
        view: () => {
          const p = panelParams.get(panelId);
          return m(iframePanelComponent, {
            url: p?.url ?? "",
            title: p?.title ?? "Tab",
            serviceName: p?.serviceName,
            panelId,
          });
        },
      });
    },
    dispose() {
      m.mount(element, null);
    },
  };
}

// The empty-state overlay sits inside the dockview container as a sibling
// to dockview's own DOM. Shown when panels.length === 0; hidden otherwise.
// Its "+" button opens the SAME dropdown buildDropdownItems() backs in the
// header's add-tab button, so the user has the same set of actions even
// when no tabs are visible.
let emptyStateOverlay: HTMLElement | null = null;
let emptyStateStatus: HTMLElement | null = null;
let emptyStateAction: HTMLButtonElement | null = null;
let emptyStateDropdown: HTMLElement | null = null;

function createEmptyStateOverlay(): HTMLElement {
  const overlay = document.createElement("div");
  overlay.className = "dockview-empty-state";
  overlay.style.position = "absolute";
  overlay.style.inset = "0";
  overlay.style.display = "none";
  overlay.style.alignItems = "center";
  overlay.style.justifyContent = "center";
  overlay.style.pointerEvents = "auto";
  overlay.style.zIndex = "1";

  const card = document.createElement("div");
  card.className = "dockview-empty-state-card";
  card.style.display = "flex";
  card.style.flexDirection = "column";
  card.style.alignItems = "center";
  card.style.padding = "32px 40px";
  card.style.background = "#fff";
  card.style.border = "1px solid #e5e7eb";
  card.style.borderRadius = "12px";
  card.style.boxShadow = "0 4px 12px rgba(0,0,0,0.06)";
  card.style.position = "relative";

  const status = document.createElement("div");
  status.className = "dockview-empty-state-status";
  status.style.marginBottom = "16px";
  status.style.color = "#374151";
  status.style.fontSize = "14px";
  status.textContent = "No tabs open";
  card.appendChild(status);

  const action = document.createElement("button");
  action.className = "dockview-empty-state-action";
  action.style.padding = "10px 20px";
  action.style.fontSize = "16px";
  action.style.fontWeight = "500";
  action.style.background = "#3b82f6";
  action.style.color = "#fff";
  action.style.border = "none";
  action.style.borderRadius = "8px";
  action.style.cursor = "pointer";
  action.textContent = "+ Open new tab";
  card.appendChild(action);

  const dropdown = document.createElement("div");
  dropdown.className = "dockview-empty-state-dropdown";
  dropdown.style.position = "absolute";
  dropdown.style.top = "calc(100% + 8px)";
  dropdown.style.left = "50%";
  dropdown.style.transform = "translateX(-50%)";
  dropdown.style.minWidth = "240px";
  dropdown.style.background = "#fff";
  dropdown.style.border = "1px solid #e5e7eb";
  dropdown.style.borderRadius = "8px";
  dropdown.style.boxShadow = "0 4px 12px rgba(0,0,0,0.08)";
  dropdown.style.padding = "4px 0";
  dropdown.style.display = "none";
  dropdown.style.zIndex = "2";
  card.appendChild(dropdown);

  action.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = dropdown.style.display !== "none";
    if (isOpen) {
      dropdown.style.display = "none";
      return;
    }
    dropdown.innerHTML = "";
    const items = buildDropdownItems();
    for (const item of items) {
      const menuItem = document.createElement("div");
      menuItem.className = "dockview-add-tab-dropdown-item";
      menuItem.textContent = item.label;
      menuItem.addEventListener("click", (clickEvent) => {
        clickEvent.stopPropagation();
        dropdown.style.display = "none";
        item.action();
      });
      dropdown.appendChild(menuItem);
      if (item.dividerAfter) {
        const divider = document.createElement("div");
        divider.style.borderTop = "1px solid #e5e7eb";
        divider.style.margin = "4px 0";
        dropdown.appendChild(divider);
      }
    }
    dropdown.style.display = "block";
  });

  // Close the dropdown when clicking elsewhere.
  document.addEventListener("click", () => {
    dropdown.style.display = "none";
  });

  overlay.appendChild(card);
  emptyStateStatus = status;
  emptyStateAction = action;
  emptyStateDropdown = dropdown;
  return overlay;
}

function updateEmptyState(): void {
  if (!emptyStateOverlay || !dockview) return;
  const isEmpty = dockview.panels.length === 0;
  emptyStateOverlay.style.display = isEmpty ? "flex" : "none";
  if (!isEmpty) {
    if (emptyStateDropdown) emptyStateDropdown.style.display = "none";
    return;
  }
  if (awaitingInitialChat) {
    if (emptyStateStatus) emptyStateStatus.textContent = "Waiting for initial chat agent...";
    if (emptyStateAction) emptyStateAction.style.display = "none";
  } else {
    if (emptyStateStatus) emptyStateStatus.textContent = "No tabs open";
    if (emptyStateAction) emptyStateAction.style.display = "";
  }
}

function initializeDockview(parentElement: HTMLElement): void {
  if (initialized) return;
  initialized = true;

  dockviewContainer = document.createElement("div");
  dockviewContainer.className = "dockview-agent-container dockview-theme-light";
  dockviewContainer.style.width = "100%";
  dockviewContainer.style.height = "100%";
  dockviewContainer.style.position = "relative";
  parentElement.appendChild(dockviewContainer);

  emptyStateOverlay = createEmptyStateOverlay();
  dockviewContainer.appendChild(emptyStateOverlay);

  // dockview-core's Scrollbar only reads event.deltaY, so mice with a dedicated
  // horizontal scroll wheel (e.g. Logitech MX Master) emit deltaX events that
  // the tab bar never reacts to. Delegate wheel here and translate deltaX into
  // scrollLeft on the tabs container; dockview's own 'scroll' listener on that
  // element will sync its internal offset, keeping the custom scrollbar thumb
  // in step.
  dockviewContainer.addEventListener(
    "wheel",
    (event: WheelEvent) => {
      if (event.deltaX === 0) return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const tabsContainer = target.closest<HTMLElement>(".dv-tabs-container");
      if (!tabsContainer || !dockviewContainer?.contains(tabsContainer)) return;
      event.preventDefault();
      tabsContainer.scrollLeft += event.deltaX;
    },
    { passive: false },
  );

  const dv = new DockviewComponent(dockviewContainer, {
    theme: themeLight,
    defaultRenderer: "always",
    defaultTabComponent: "custom",
    createComponent(options) {
      const params = (options as unknown as { params?: PanelParams }).params ?? panelParams.get(options.id);

      switch (options.name) {
        case "chat":
          return createMithrilRenderer(ChatPanel, {
            agentId: params?.chatAgentId ?? params?.agentId ?? getPrimaryAgentId(),
          });

        case "iframe":
          // Pull live values out of ``panelParams`` on every redraw so an
          // agent-driven ``replace-url`` (which mutates the stored
          // params) re-renders the iframe with the new src instead of
          // staying frozen on the initial url captured at mount time.
          return createReactiveIframeRenderer(options.id);

        case "subagent":
          return createMithrilRenderer(SubagentView, {
            agentId: params?.agentId ?? getPrimaryAgentId(),
            subagentSessionId: params?.subagentSessionId ?? "",
          });

        default:
          return createMithrilRenderer(ChatPanel, { agentId: getPrimaryAgentId() });
      }
    },
    createTabComponent(options) {
      return createCustomTab(options);
    },
    createLeftHeaderActionComponent() {
      return createAddTabButton();
    },
  });

  dockview = dv;

  // Listen for layout changes and auto-save
  dv.api.onDidLayoutChange(() => {
    scheduleSave();
  });

  // Clean up params on panel removal. We DON'T reopen anything when
  // panels.length hits zero -- instead the empty-state overlay (see
  // below) appears, giving the user the same "+" dropdown actions the
  // dockview header normally hosts. This is the user-visible escape from
  // the previous "system-services keeps coming back" behavior.
  dv.api.onDidRemovePanel((panel) => {
    panelParams.delete(panel.id);
    updateEmptyState();
  });
  dv.api.onDidAddPanel(() => {
    updateEmptyState();
  });

  // While awaitingInitialChat is true, every agents_updated event is
  // another chance for the bootstrap-created chat agent to show up.
  agentsUpdatedListener = () => {
    if (awaitingInitialChat && openInitialChatTab()) {
      awaitingInitialChat = false;
      updateEmptyState();
    }
  };
  addAgentsUpdatedListener(agentsUpdatedListener);

  // Agent-driven layout ops arrive as {type: "layout_op", op, args} on
  // the workspace-server WebSocket. ``scripts/layout.py`` is the source
  // of those messages; per-op handlers below dispatch on ``event.op``.
  _layoutOpListener = (event: LayoutOpEvent) => {
    void handleLayoutOp(event);
  };
  addLayoutOpListener(_layoutOpListener);

  // Load saved layout or create default
  loadLayout().then((saved) => {
    let savedHadAnyPanels = false;
    if (saved) {
      for (const [id, params] of Object.entries(saved.panelParams)) {
        panelParams.set(id, params);
      }
      try {
        dv.fromJSON(saved.dockview);
      } catch {
        panelParams.clear();
      }
      savedHadAnyPanels = dv.panels.length > 0;
      // Strip any panels that point at the is_primary services agent.
      // Older saved layouts (or layouts saved by the previous code path
      // that auto-opened the primary agent's chat) may carry a chat-
      // <services-agent-id> panel; we don't want to surface that ever.
      const primaryId = getPrimaryAgentId();
      if (primaryId) {
        for (const panel of dv.panels.slice()) {
          const params = panelParams.get(panel.id);
          const targetId = params?.chatAgentId ?? params?.agentId;
          if (targetId === primaryId) {
            dv.removePanel(panel);
          }
        }
      }
    }

    // Auto-open the initial chat tab when:
    //   - no saved layout exists (first ever load), OR
    //   - the saved layout existed but all its panels were services-agent
    //     panels we just stripped above.
    // If the saved layout was a non-empty layout that the user
    // intentionally emptied (savedHadAnyPanels=false because saved
    // existed but had zero panels), we respect that and leave the
    // empty state visible.
    const shouldAutoOpen = saved === null || (savedHadAnyPanels && dv.panels.length === 0);
    if (shouldAutoOpen) {
      if (!openInitialChatTab()) {
        awaitingInitialChat = true;
      }
    }
    updateEmptyState();
  });
}

async function executeDestroy(agentId: string, panelId: string): Promise<void> {
  // Destroy the target agent
  try {
    const response = await fetch(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/destroy`), {
      method: "POST",
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const detail = (data as { detail?: string }).detail ?? "Unknown error";
      alert(`Failed to destroy agent: ${detail}`);
      return;
    }
  } catch (e) {
    alert(`Failed to destroy agent: ${(e as Error).message}`);
    return;
  }

  // Remove from local state
  removeAgentLocally(agentId);

  // Remove the panel from dockview
  if (dockview) {
    const panel = dockview.panels.find((p) => p.id === panelId);
    if (panel) {
      dockview.removePanel(panel);
    }
  }

  m.redraw();
}

export const DockviewWorkspace: m.Component = {
  oncreate(vnode: m.VnodeDOM) {
    const wrapper = vnode.dom as HTMLElement;
    initializeDockview(wrapper);
  },

  onupdate(_vnode: m.VnodeDOM) {
    // Resize the dockview when the container changes
    if (dockview && dockviewContainer) {
      requestAnimationFrame(() => {
        if (dockviewContainer) {
          const rect = dockviewContainer.getBoundingClientRect();
          dockview!.layout(rect.width, rect.height);
        }
      });
    }
  },

  view() {
    return m(
      "div",
      {
        class: "dockview-workspace",
        style: "width: 100%; height: 100%;",
      },
      [
        showNewChatModal
          ? m(CreateAgentModal, {
              mode: "chat",
              onCreated(newAgentId: string, newAgentName: string) {
                showNewChatModal = false;
                focusOrCreateChatPanel(newAgentId, newAgentName);
              },
              onCancel() {
                showNewChatModal = false;
              },
            })
          : null,

        showNewAgentModal
          ? m(CreateAgentModal, {
              mode: "worktree",
              onCreated(newAgentId: string, newAgentName: string) {
                showNewAgentModal = false;
                focusOrCreateChatPanel(newAgentId, newAgentName);
              },
              onCancel() {
                showNewAgentModal = false;
              },
            })
          : null,

        showDestroyDialog && destroyTargetAgentId && destroyTargetAgentName
          ? m(DestroyConfirmDialog, {
              agentName: destroyTargetAgentName,
              onConfirm() {
                showDestroyDialog = false;
                const targetId = destroyTargetAgentId!;
                const panelId = destroyTargetPanelId!;
                destroyTargetAgentId = null;
                destroyTargetAgentName = null;
                destroyTargetPanelId = null;
                executeDestroy(targetId, panelId);
              },
              onCancel() {
                showDestroyDialog = false;
                destroyTargetAgentId = null;
                destroyTargetAgentName = null;
                destroyTargetPanelId = null;
              },
            })
          : null,

        showShareModal && shareServiceName
          ? m(ShareModal, {
              serviceName: shareServiceName,
              onClose() {
                showShareModal = false;
                shareServiceName = null;
              },
            })
          : null,
      ],
    );
  },
};
