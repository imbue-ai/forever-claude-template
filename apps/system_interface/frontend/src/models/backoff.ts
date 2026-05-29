/**
 * Exponential reconnect backoff with a cap and jitter.
 *
 * Used by the WebSocket (AgentManager) and SSE (StreamingMessage) reconnect
 * paths so a down backend is retried with growing delays instead of a fixed
 * hammering interval. Jitter spreads reconnects across clients so a backend
 * restart does not trigger a synchronized thundering herd.
 */

export const RECONNECT_BASE_MS = 1000;
export const RECONNECT_CAP_MS = 30000;
const JITTER_RATIO = 0.2;

/**
 * Delay in ms for a given 0-based consecutive-failure count: base * 2^attempt,
 * capped at RECONNECT_CAP_MS, then perturbed by +/-JITTER_RATIO. `random` is
 * injectable so tests can pin the jitter.
 */
export function computeBackoffDelay(attempt: number, random: () => number = Math.random): number {
  const exponential = RECONNECT_BASE_MS * 2 ** attempt;
  const capped = Math.min(exponential, RECONNECT_CAP_MS);
  const jitter = capped * JITTER_RATIO * (random() * 2 - 1);
  return Math.max(0, Math.round(capped + jitter));
}

/**
 * Stateful per-connection backoff. `nextDelay` returns the delay for the
 * current attempt and advances the counter; `reset` returns to the base delay
 * and should be called once a connection succeeds.
 */
export class ReconnectBackoff {
  private attempt = 0;

  constructor(private readonly random: () => number = Math.random) {}

  nextDelay(): number {
    const delay = computeBackoffDelay(this.attempt, this.random);
    this.attempt += 1;
    return delay;
  }

  reset(): void {
    this.attempt = 0;
  }
}
