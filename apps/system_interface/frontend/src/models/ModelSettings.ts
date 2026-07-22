/**
 * Per-agent model + fast-mode state for the composer model picker.
 *
 * The backend exposes the agent's Claude Code selection (read from its
 * settings.json) and applies changes by sending `/model` / `/fast` slash
 * commands to the running session (see server.py). This module fetches that
 * state, caches it per agent, and posts changes -- optimistically reflecting the
 * new selection right away, then reconciling against the agent's real settings a
 * moment later (Claude Code persists the command asynchronously).
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

const settingsByAgent = new Map<string, ModelSettings>();
const inFlightFetch = new Set<string>();

// Monotonic per-agent counter bumped on each model/fast change. A reconcile (or a
// POST-failure refresh) only commits its result while its generation is still the
// latest, so a newer change supersedes an older one's in-flight poll instead of
// letting a stale read flip the picker back.
const changeGeneration = new Map<string, number>();

function nextGeneration(agentId: string): number {
  const generation = (changeGeneration.get(agentId) ?? 0) + 1;
  changeGeneration.set(agentId, generation);
  return generation;
}

function isCurrentGeneration(agentId: string, generation: number): boolean {
  return changeGeneration.get(agentId) === generation;
}

// Claude Code persists a `/model` / `/fast` change to settings.json only after it
// processes the command -- near-instant when idle, but delayed when it is
// mid-turn (the command queues). So after posting, poll until settings.json
// reflects the change rather than reading once (a single early read would show
// the stale value and revert the optimistic pick). Give up after the window and
// accept whatever is on disk -- if the change never landed (e.g. an org-gated
// `/fast on` was refused), the true state is the honest thing to show.
const RECONCILE_POLL_INTERVAL_MS = 700;
const RECONCILE_MAX_ATTEMPTS = 8;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

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
    if (settings !== null) {
      settingsByAgent.set(agentId, settings);
      m.redraw();
    }
  } finally {
    inFlightFetch.delete(agentId);
  }
}

/** Commit `settings` as the agent's true state, but only if `generation` is still
 *  the latest change -- a newer change's reconcile owns the display otherwise. */
function commitIfCurrent(agentId: string, generation: number, settings: ModelSettings): void {
  if (isCurrentGeneration(agentId, generation)) {
    settingsByAgent.set(agentId, settings);
    m.redraw();
  }
}

/** Poll settings.json until `isSettled` holds (the change landed), then commit
 *  the real state; give up after the window and commit whatever is on disk. The
 *  optimistic value stays displayed until then -- an unsettled read never
 *  overwrites it, so the picker doesn't flip back to the stale value mid-apply.
 *  A superseding change (newer generation) abandons this loop. */
async function reconcileAfterChange(
  agentId: string,
  generation: number,
  isSettled: (settings: ModelSettings) => boolean,
): Promise<void> {
  let latest: ModelSettings | null = null;
  for (let attempt = 0; attempt < RECONCILE_MAX_ATTEMPTS; attempt++) {
    await sleep(RECONCILE_POLL_INTERVAL_MS);
    if (!isCurrentGeneration(agentId, generation)) {
      return;
    }
    latest = await requestModelSettings(agentId);
    if (!isCurrentGeneration(agentId, generation)) {
      return;
    }
    if (latest !== null && isSettled(latest)) {
      commitIfCurrent(agentId, generation, latest);
      return;
    }
  }
  // Window elapsed without settling -- show the true on-disk state (the change
  // may have been refused, e.g. an org-gated fast mode).
  if (latest !== null) {
    commitIfCurrent(agentId, generation, latest);
  }
}

/** After a change POST fails (e.g. the backend returns 500 because the agent is
 *  stopped/restarting and could not receive the command), the optimistic value is
 *  a lie -- the change never landed. Restore the true on-disk state so the picker
 *  stops showing a selection the agent never received. */
async function restoreTrueState(agentId: string, generation: number): Promise<void> {
  const settings = await requestModelSettings(agentId);
  if (settings !== null) {
    commitIfCurrent(agentId, generation, settings);
  }
}

export async function setModel(agentId: string, modelId: string): Promise<void> {
  const generation = nextGeneration(agentId);
  const current = settingsByAgent.get(agentId);
  if (current) {
    // Optimistic: reflect the pick immediately so the picker feels responsive.
    // fast_mode_supported follows the newly chosen model, and fast mode cannot
    // be on for a model that does not support it.
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
  try {
    await m.request({
      method: "POST",
      url: apiUrl("/api/agents/:agentId/model"),
      params: { agentId },
      body: { model: modelId },
    });
  } catch (error) {
    console.warn(`Failed to set model for agent ${agentId}`, error);
    await restoreTrueState(agentId, generation);
    return;
  }
  await reconcileAfterChange(agentId, generation, (settings) => baseAlias(settings.model) === baseAlias(modelId));
}

export async function setFastMode(agentId: string, enabled: boolean): Promise<void> {
  const generation = nextGeneration(agentId);
  const current = settingsByAgent.get(agentId);
  if (current) {
    settingsByAgent.set(agentId, { ...current, fast_mode: enabled });
    m.redraw();
  }
  try {
    await m.request({
      method: "POST",
      url: apiUrl("/api/agents/:agentId/fast"),
      params: { agentId },
      body: { enabled },
    });
  } catch (error) {
    console.warn(`Failed to set fast mode for agent ${agentId}`, error);
    await restoreTrueState(agentId, generation);
    return;
  }
  await reconcileAfterChange(agentId, generation, (settings) => settings.fast_mode === enabled);
}
