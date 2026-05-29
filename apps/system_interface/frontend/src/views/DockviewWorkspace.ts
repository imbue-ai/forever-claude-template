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
import { IframePanel, reloadIframesForService } from "./IframePanel";
import { SubagentView } from "./SubagentView";
import { CreateAgentModal } from "./CreateAgentModal";
import { DestroyConfirmDialog } from "./DestroyConfirmDialog";
import { ShareModal } from "./ShareModal";
import { apiUrl, getPrimaryAgentId } from "../base-path";
import {
  addAgentsUpdatedListener,
  addRefreshServiceListener,
  getAgentById,
  getAgents,
  getApplications,
  getProtoAgents,
  removeAgentLocally,
  type AgentsUpdatedListener,
  type RefreshServiceListener,
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
// same origin as the dockview UI itself. The system_interface's service
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
  // Drives both the WS-driven `refresh_service` broadcast match and the
  // presence of the per-tab Refresh button.
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
// The dialog stays mounted across the destroy request so a failure can be
// surfaced inline rather than via a blocking alert().
let destroyInProgress = false;
let destroyError: string | null = null;

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
let _refreshServiceListener: RefreshServiceListener | null = null;
let initialized = false;

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
            destroyInProgress = false;
            destroyError = null;
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

export function openIframeTabForAgent(agentId: string, url: string, title: string): void {
  if (!dockview) return;
  const existing = dockview.panels.find((p) => {
    const pp = panelParams.get(p.id);
    return pp?.panelType === "iframe" && pp.agentId === agentId && pp.url === url;
  });
  if (existing) {
    if (!existing.api.isActive) {
      dockview.setActivePanel(existing);
    }
    return;
  }
  const panelId = `iframe-agent-${agentId}-${Date.now()}`;
  const params: PanelParams = { panelType: "iframe", agentId, url, title };
  panelParams.set(panelId, params);
  dockview.addPanel({
    id: panelId,
    component: "iframe",
    title,
    params,
  });
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
          return createMithrilRenderer(IframePanel, {
            url: params?.url ?? "",
            title: params?.title ?? "Tab",
            serviceName: params?.serviceName,
          });

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

  // Agent-triggered refresh: reload every open iframe tab whose
  // data-service-name attribute matches the service_name the agent named.
  // This arrives over the existing system_interface WebSocket as
  // {type: "refresh_service", service_name}.
  _refreshServiceListener = (serviceName: string) => {
    reloadIframesForService(serviceName);
  };
  addRefreshServiceListener(_refreshServiceListener);

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
      // Strip any chat panels that point at the is_primary services agent.
      // Older saved layouts (or layouts saved by the previous code path
      // that auto-opened the primary agent's chat) may carry a chat-
      // <services-agent-id> panel; we don't want to surface that ever.
      //
      // This MUST be limited to chat panels. Iframe tabs (terminals,
      // applications, custom URLs) opened via openIframeTab() set
      // `agentId` to the primary agent id as a placeholder owner, so a
      // bare `agentId === primaryId` check would wrongly strip every
      // terminal/application/URL tab on each restore.
      const primaryId = getPrimaryAgentId();
      if (primaryId) {
        for (const panel of dv.panels.slice()) {
          const params = panelParams.get(panel.id);
          if (params?.panelType !== "chat") continue;
          const targetId = params.chatAgentId ?? params.agentId;
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

/**
 * Destroy the target agent and tear down its panel. Throws on failure so the
 * caller can surface the error inline in the still-open confirm dialog.
 */
async function executeDestroy(agentId: string, panelId: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/destroy`), {
    method: "POST",
  }).catch(() => null);
  if (response === null) {
    throw new Error("Failed to destroy agent: could not reach the server");
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const detail = (data as { detail?: string }).detail ?? "Unknown error";
    throw new Error(`Failed to destroy agent: ${detail}`);
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

  // No onupdate-driven layout: DockviewComponent installs its own
  // ResizeObserver on the container (disableAutoResizing defaults to false), so
  // it already re-lays-out when the container changes size. Calling layout() on
  // every Mithril redraw (this component re-renders on every global redraw, as
  // its view only hosts modals) was redundant work.

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
              busy: destroyInProgress,
              error: destroyError,
              async onConfirm() {
                if (destroyInProgress) return;
                const targetId = destroyTargetAgentId!;
                const panelId = destroyTargetPanelId!;
                destroyInProgress = true;
                destroyError = null;
                m.redraw();
                try {
                  await executeDestroy(targetId, panelId);
                } catch (e) {
                  // Keep the dialog open and show the error inline so the user
                  // can retry or cancel.
                  destroyInProgress = false;
                  destroyError = (e as Error).message;
                  m.redraw();
                  return;
                }
                showDestroyDialog = false;
                destroyInProgress = false;
                destroyTargetAgentId = null;
                destroyTargetAgentName = null;
                destroyTargetPanelId = null;
              },
              onCancel() {
                if (destroyInProgress) return;
                showDestroyDialog = false;
                destroyError = null;
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
