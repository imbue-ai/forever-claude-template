import { describe, expect, it } from "vitest";
import { computePanelsToCloseForRemovedAgents, type PanelParams } from "./DockviewWorkspace";

// Matches the prefix returned by getTerminalUrl(); agent-terminal URLs are
// this prefix plus the ttyd `arg=agent` dispatch key.
const TERMINAL_PREFIX = "/service/terminal/";

function agentTerminalUrl(agentName: string): string {
  return `${TERMINAL_PREFIX}?arg=_&arg=agent&arg=${encodeURIComponent(agentName)}`;
}

function chatPanel(agentId: string): { panelId: string; params: PanelParams } {
  return { panelId: `chat-${agentId}`, params: { panelType: "chat", agentId, chatAgentId: agentId } };
}

function agentTerminalPanel(agentId: string, agentName: string): { panelId: string; params: PanelParams } {
  return {
    panelId: `iframe-agent-${agentId}-1`,
    params: { panelType: "iframe", agentId, url: agentTerminalUrl(agentName), title: `${agentName} terminal` },
  };
}

describe("computePanelsToCloseForRemovedAgents", () => {
  it("closes a chat tab whose agent was seen alive and is now gone", () => {
    const result = computePanelsToCloseForRemovedAgents(
      [chatPanel("a")],
      TERMINAL_PREFIX,
      new Set<string>(), // a is no longer live
      new Set(["a"]), // but was seen earlier this session
    );
    expect(result).toEqual(["chat-a"]);
  });

  it("closes an agent-terminal tab whose agent was destroyed", () => {
    const result = computePanelsToCloseForRemovedAgents(
      [agentTerminalPanel("a", "alice")],
      TERMINAL_PREFIX,
      new Set<string>(),
      new Set(["a"]),
    );
    expect(result).toEqual(["iframe-agent-a-1"]);
  });

  it("keeps tabs for agents that are still alive", () => {
    const result = computePanelsToCloseForRemovedAgents(
      [chatPanel("a"), agentTerminalPanel("a", "alice")],
      TERMINAL_PREFIX,
      new Set(["a"]),
      new Set(["a"]),
    );
    expect(result).toEqual([]);
  });

  it("does not close a tab for an agent that hasn't been seen yet", () => {
    // A chat tab restored from a saved layout before the first
    // agents_updated arrives: the agent isn't in the (empty) live set, but
    // it was also never observed alive, so we must not tear the tab down.
    const result = computePanelsToCloseForRemovedAgents(
      [chatPanel("a")],
      TERMINAL_PREFIX,
      new Set<string>(),
      new Set<string>(),
    );
    expect(result).toEqual([]);
  });

  it("ignores non-agent tabs even when their placeholder owner is absent", () => {
    // Generic terminals, applications, and ad-hoc URLs carry the primary
    // agent id as a placeholder owner; they must never be closed by this
    // reconciliation. Subagent views are likewise not destroyable-agent tabs.
    const primaryId = "primary";
    const genericTerminal: { panelId: string; params: PanelParams } = {
      panelId: "iframe-terminal-1",
      params: { panelType: "iframe", agentId: primaryId, url: TERMINAL_PREFIX, title: "terminal" },
    };
    const appTab: { panelId: string; params: PanelParams } = {
      panelId: "iframe-primary-2",
      params: { panelType: "iframe", agentId: primaryId, url: "/service/web/", title: "web", serviceName: "web" },
    };
    const subagentTab: { panelId: string; params: PanelParams } = {
      panelId: "subagent-a-sess",
      params: { panelType: "subagent", agentId: "a", subagentSessionId: "sess", title: "sub" },
    };
    const result = computePanelsToCloseForRemovedAgents(
      [genericTerminal, appTab, subagentTab],
      TERMINAL_PREFIX,
      new Set<string>(),
      new Set(["primary", "a"]),
    );
    expect(result).toEqual([]);
  });

  it("closes only the destroyed agent's tabs in a mixed layout", () => {
    const result = computePanelsToCloseForRemovedAgents(
      [chatPanel("alive"), chatPanel("dead"), agentTerminalPanel("dead", "deadname")],
      TERMINAL_PREFIX,
      new Set(["alive"]),
      new Set(["alive", "dead"]),
    );
    expect(result).toEqual(["chat-dead", "iframe-agent-dead-1"]);
  });
});
