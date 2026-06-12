import m from "mithril";
import { DockviewWorkspace } from "./DockviewWorkspace";
import { ClaudeLoginModal } from "./ClaudeLoginModal";
import { MemoryPressureBanner } from "./MemoryPressureBanner";
import { isLoginModalOpen, closeLoginModal } from "../models/ClaudeAuth";

export function App(): m.Component {
  return {
    view() {
      return m(
        "div",
        { class: "app-layout flex flex-col", style: "height: calc(100vh - var(--minds-titlebar-height, 0px))" },
        [
          m("div", { class: "minds-titlebar-spacer" }),
          // Full-width pressure strip above the workspace; renders nothing (zero
          // layout impact) unless the watchdog reports sustained memory pressure.
          m(MemoryPressureBanner),
          m("div", { class: "app-main flex flex-1 min-w-80" }, [m(DockviewWorkspace)]),
          // Claude auth is mind-global, so the login modal is a single
          // app-level instance driven by global auth state -- not one per
          // ChatPanel. It opens when any agent surfaces an auth-error.
          isLoginModalOpen() ? m(ClaudeLoginModal, { onDismiss: closeLoginModal }) : null,
        ],
      );
    },
  };
}
