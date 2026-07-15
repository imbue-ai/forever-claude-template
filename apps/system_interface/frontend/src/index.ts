import { llmApi } from "./llm-api";
import type { LlmApi } from "./llm-api";
import { runHook } from "./hooks";
import { getPluginRouteMithrilComponents } from "./plugin-routes";
import { getBasePath } from "./base-path";
import { initAgentManager } from "./models/AgentManager";
import { initQueuedMessageIdleClearing, initReconnectingMessageExpiry } from "./models/PendingMessages";
import m from "mithril";
import "./style.css";
import { App } from "./views/App";

declare global {
  interface Window {
    $llm: LlmApi;
  }
  var $llm: LlmApi;
}

window.$llm = llmApi;

function getEffectiveRoutePrefix(): string {
  // When served through the desktop client proxy, the <base> tag contains
  // the forwarding prefix (e.g., /forwarding/{agentId}/web/). Use this as
  // the Mithril route prefix so pushState preserves the correct URL in the
  // browser history stack (enabling back/forward navigation).
  const baseEl = document.querySelector("base[href]");
  if (baseEl) {
    const href = baseEl.getAttribute("href") ?? "";
    if (href.includes("/forwarding/")) {
      return href.replace(/\/+$/, "");
    }
  }
  return getBasePath();
}

async function bootstrap(): Promise<void> {
  m.route.prefix = getEffectiveRoutePrefix();
  initAgentManager();
  // Backstop that drops an optimistic "queued" bubble once its agent returns to
  // idle (the message was edited away or dropped, so it will never reconcile).
  initQueuedMessageIdleClearing();
  // Backstop that gives up on a "reconnecting" message once it has been failing
  // to reach the backend for longer than RECONNECTING_GIVE_UP_MS (the connection
  // never recovered), so a permanently-dead backend cannot leave a stuck bubble.
  initReconnectingMessageExpiry();
  const rootElement = document.getElementById("app");
  if (rootElement) {
    const pluginRoutes = getPluginRouteMithrilComponents();
    const appResolver: m.RouteResolver = { render: () => m(App) };
    m.route(rootElement, "/", {
      "/": appResolver,
      ...pluginRoutes,
    });
    await runHook("ready");
  }
}

window.addEventListener("load", bootstrap);
