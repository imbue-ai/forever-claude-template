/**
 * Mobile replacement for the dockview tab strip.
 *
 * On small screens (see isMobileViewport) the dockview header is hidden and
 * this bar renders above the workspace instead: a hamburger button at the top
 * left and the active tab's title beside it. The hamburger opens a single
 * bottom-sheet menu with everything tab-related: the open tabs (tap to
 * switch, with close/destroy actions) followed by the same "open new" items
 * as the desktop "+" dropdown. A bottom sheet is the mobile idiom for this
 * menu: full-width, thumb-reachable, and scrollable when long.
 *
 * The component is pure presentation -- panel state, actions, and menu items
 * are supplied by DockviewWorkspace, which stays the single owner of dockview
 * bookkeeping.
 */

import m from "mithril";

export interface MobileTabInfo {
  panelId: string;
  title: string;
  isActive: boolean;
  // What the row's trash action destroys: an mngr agent, a tmux session, or
  // nothing (plain close-only tabs, and the primary agent which must never
  // be destroyed).
  destroyKind: "agent" | "terminal" | null;
}

export interface MobileAddMenuItem {
  label: string;
  action: () => void;
  dividerAfter?: boolean;
  disabled?: boolean;
  disabledReason?: string;
}

export interface MobileTabBarAttrs {
  tabs: MobileTabInfo[];
  // Built lazily on each redraw while the menu is open, so fleet refreshes
  // (browsers, terminals) show up as soon as they land.
  buildAddItems: () => MobileAddMenuItem[];
  // Fired when the menu opens; the owner kicks off its async fleet refreshes
  // here (mirroring the desktop dropdown's open handler).
  onMenuOpen: () => void;
  onSelectTab: (panelId: string) => void;
  onCloseTab: (panelId: string) => void;
  onDestroyTab: (panelId: string) => void;
}

// Media query mirroring the CSS breakpoint in responsive.css. Kept in one
// place so the JS-rendered bar and the CSS that hides the dockview header can
// never disagree.
const MOBILE_VIEWPORT_QUERY = "(max-width: 768px)";

let mobileQuery: MediaQueryList | null = null;

/** Whether the viewport is phone-sized. Subscribes mithril to breakpoint
 *  crossings on first use so the bar mounts/unmounts on resize. */
export function isMobileViewport(): boolean {
  if (mobileQuery === null) {
    mobileQuery = window.matchMedia(MOBILE_VIEWPORT_QUERY);
    mobileQuery.addEventListener("change", () => m.redraw());
  }
  return mobileQuery.matches;
}

const HAMBURGER_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/></svg>';

const CLOSE_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>';

const TRASH_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';

export function MobileTabBar(): m.Component<MobileTabBarAttrs> {
  let menuOpen = false;

  function closeMenu(): void {
    menuOpen = false;
  }

  function renderTabRows(attrs: MobileTabBarAttrs): m.Children[] {
    if (attrs.tabs.length === 0) {
      return [m("div", { class: "mobile-sheet-empty" }, "No tabs open")];
    }
    return attrs.tabs.map((tab) =>
      m(
        "div",
        {
          key: tab.panelId,
          class: tab.isActive ? "mobile-sheet-row mobile-sheet-row--active" : "mobile-sheet-row",
          onclick: () => {
            closeMenu();
            attrs.onSelectTab(tab.panelId);
          },
        },
        [
          m("span", { class: "mobile-sheet-row-label" }, tab.title),
          tab.destroyKind !== null
            ? m(
                "button",
                {
                  type: "button",
                  class: "mobile-sheet-row-action mobile-sheet-row-action--destructive",
                  title: tab.destroyKind === "agent" ? "Destroy agent" : "Destroy terminal",
                  "aria-label": tab.destroyKind === "agent" ? "Destroy agent" : "Destroy terminal",
                  onclick: (event: MouseEvent) => {
                    event.stopPropagation();
                    closeMenu();
                    attrs.onDestroyTab(tab.panelId);
                  },
                },
                m.trust(TRASH_SVG),
              )
            : null,
          m(
            "button",
            {
              type: "button",
              class: "mobile-sheet-row-action",
              title: "Close tab",
              "aria-label": "Close tab",
              onclick: (event: MouseEvent) => {
                event.stopPropagation();
                attrs.onCloseTab(tab.panelId);
              },
            },
            m.trust(CLOSE_SVG),
          ),
        ],
      ),
    );
  }

  function renderAddRows(attrs: MobileTabBarAttrs): m.Children[] {
    const items = attrs.buildAddItems();
    const rows: m.Children[] = [];
    for (const item of items) {
      rows.push(
        m(
          "div",
          {
            class: item.disabled ? "mobile-sheet-row mobile-sheet-row--disabled" : "mobile-sheet-row",
            onclick: () => {
              if (item.disabled) {
                if (item.disabledReason) alert(item.disabledReason);
                return;
              }
              closeMenu();
              item.action();
            },
          },
          m("span", { class: "mobile-sheet-row-label" }, item.label),
        ),
      );
      if (item.dividerAfter) {
        rows.push(m("div", { class: "mobile-sheet-divider" }));
      }
    }
    return rows;
  }

  function renderMenuSheet(attrs: MobileTabBarAttrs): m.Children {
    return [
      m("div", { class: "mobile-sheet-backdrop", onclick: closeMenu }),
      m("div", { class: "mobile-sheet" }, [
        m("div", { class: "mobile-sheet-grabber" }),
        m("div", { class: "mobile-sheet-rows" }, [
          m("div", { class: "mobile-sheet-title" }, "Tabs"),
          // The tab rows stay a nested array: mithril normalizes it into its
          // own fragment, which keeps the keyed rows (by panel id) uniformly
          // keyed among themselves without keying these section siblings.
          renderTabRows(attrs),
          m("div", { class: "mobile-sheet-divider" }),
          m("div", { class: "mobile-sheet-title" }, "Open new"),
          ...renderAddRows(attrs),
        ]),
      ]),
    ];
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const active = attrs.tabs.find((tab) => tab.isActive);
      return m("div", { class: "mobile-tab-bar-root" }, [
        m("div", { class: "mobile-tab-bar" }, [
          m(
            "button",
            {
              type: "button",
              class: "mobile-tab-bar-menu-button",
              title: "Menu",
              "aria-label": "Menu",
              onclick: () => {
                menuOpen = !menuOpen;
                if (menuOpen) {
                  attrs.onMenuOpen();
                }
              },
            },
            m.trust(HAMBURGER_SVG),
          ),
          m("span", { class: "mobile-tab-bar-title" }, active?.title ?? "No tabs open"),
        ]),
        menuOpen ? renderMenuSheet(attrs) : null,
      ]);
    },
  };
}
