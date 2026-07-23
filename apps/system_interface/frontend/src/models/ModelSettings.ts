/**
 * Per-agent model + fast-mode state for the composer model picker.
 *
 * The backend exposes the agent's Claude Code selection (read from its
 * settings.json) and applies changes by sending `/model` / `/fast` slash
 * commands to the running session (see server.py). This module caches that
 * state per agent, reflects a pick optimistically so the control feels
 * responsive, and applies changes through a per-agent single-flight chain: at
 * most one change request is in flight for an agent at a time, and they run in
 * click order. That is what keeps rapid clicks correct -- without it, the
 * browser fires the requests concurrently and the threaded backend delivers the
 * `/model` / `/fast` commands to Claude in a nondeterministic order, so the
 * agent can end up in the opposite state from the last click. When the chain
 * drains, we read settings.json once to reconcile the display with reality
 * (catching a refused change, e.g. an org-gated `/fast on`).
 */

import m from "mithril";
import { apiUrl } from "../base-path";

export interface ModelOption {
  id: string;
  label: string;
  supports_fast_mode: boolean;
}

export interface ModelSettings {
  model: string;
  fast_mode: boolean;
  fast_mode_supported: boolean;
  options: ModelOption[];
}

// The state the picker displays -- seeded from settings.json, updated
// optimistically on a pick, and reconciled back to the truth once a change
// settles.
const settingsByAgent = new Map<string, ModelSettings>();
const inFlightFetch = new Set<string>();

// Tail of each agent's apply chain. A new change appends to it, so changes for
// one agent run strictly in click order, one at a time.
const applyChainByAgent = new Map<string, Promise<void>>();

/** Bare alias of a model string, matching the backend's `base_alias`
 *  (`opus[1m]` -> `opus`), so a stored `opus` or `opus[1m]` both map to the
 *  Opus catalog option. */
export function baseAlias(model: string): string {
  return model.split("[")[0].trim().toLowerCase();
}

export function getModelSettings(agentId: string): ModelSettings | null {
  return settingsByAgent.get(agentId) ?? null;
}

/** The catalog option currently selected for the agent, matched by bare alias. */
export function getSelectedOption(agentId: string): ModelOption | null {
  const settings = settingsByAgent.get(agentId);
  if (!settings) {
    return null;
  }
  const currentAlias = baseAlias(settings.model);
  return settings.options.find((option) => baseAlias(option.id) === currentAlias) ?? null;
}

async function requestModelSettings(agentId: string): Promise<ModelSettings | null> {
  try {
    return await m.request<ModelSettings>({
      method: "GET",
      url: apiUrl("/api/agents/:agentId/model-settings"),
      params: { agentId },
    });
  } catch (error) {
    console.warn(`Failed to load model settings for agent ${agentId}`, error);
    return null;
  }
}

export async function fetchModelSettings(agentId: string): Promise<void> {
  if (inFlightFetch.has(agentId)) {
    return;
  }
  inFlightFetch.add(agentId);
  try {
    const settings = await requestModelSettings(agentId);
    // Don't clobber an optimistic pick that is still being applied -- the apply
    // chain's own settle-read owns the display while it is active.
    if (settings !== null && !applyChainByAgent.has(agentId)) {
      settingsByAgent.set(agentId, settings);
      m.redraw();
    }
  } finally {
    inFlightFetch.delete(agentId);
  }
}

/** Queue `apply` (a single change POST) onto the agent's chain so it runs after
 *  any in-flight change, in click order. When the chain drains -- this task is
 *  still its tail once it finishes -- read settings.json once to reconcile the
 *  display with the agent's real state. */
function enqueueApply(agentId: string, apply: () => Promise<void>): void {
  const previous = applyChainByAgent.get(agentId) ?? Promise.resolve();
  const next = previous.then(apply, apply);
  applyChainByAgent.set(agentId, next);
  void next.then(async () => {
    if (applyChainByAgent.get(agentId) !== next) {
      // A newer change is queued behind us; it owns the settle-read.
      return;
    }
    applyChainByAgent.delete(agentId);
    const settings = await requestModelSettings(agentId);
    if (settings !== null && !applyChainByAgent.has(agentId)) {
      settingsByAgent.set(agentId, settings);
      m.redraw();
    }
  });
}

export function setModel(agentId: string, modelId: string): void {
  const current = settingsByAgent.get(agentId);
  if (current) {
    // Optimistic: reflect the pick immediately. fast_mode_supported follows the
    // newly chosen model, and fast mode cannot be on for a model that does not
    // support it (Claude auto-disables it on the switch).
    const chosen = current.options.find((option) => option.id === modelId);
    const supportsFast = chosen?.supports_fast_mode ?? false;
    settingsByAgent.set(agentId, {
      ...current,
      model: modelId,
      fast_mode_supported: supportsFast,
      fast_mode: supportsFast ? current.fast_mode : false,
    });
    m.redraw();
  }
  enqueueApply(agentId, async () => {
    try {
      await m.request({
        method: "POST",
        url: apiUrl("/api/agents/:agentId/model"),
        params: { agentId },
        body: { model: modelId },
      });
    } catch (error) {
      // The settle-read reconciles the display back to the truth.
      console.warn(`Failed to set model for agent ${agentId}`, error);
    }
  });
}

export function setFastMode(agentId: string, enabled: boolean): void {
  const current = settingsByAgent.get(agentId);
  if (current) {
    settingsByAgent.set(agentId, { ...current, fast_mode: enabled });
    m.redraw();
  }
  enqueueApply(agentId, async () => {
    try {
      await m.request({
        method: "POST",
        url: apiUrl("/api/agents/:agentId/fast"),
        params: { agentId },
        body: { enabled },
      });
    } catch (error) {
      console.warn(`Failed to set fast mode for agent ${agentId}`, error);
    }
  });
}
