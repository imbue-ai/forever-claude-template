/**
 * Safe JSON parsing for WebSocket / SSE message payloads.
 *
 * A malformed frame must not throw out of an `onmessage` handler: the
 * connection itself is healthy, so the bad frame is dropped and the listener
 * keeps running. Returns null on parse failure.
 */
export function parseJsonMessage<T>(raw: string): T | null {
  try {
    return JSON.parse(raw) as T;
  } catch (error) {
    console.warn("Discarding malformed WebSocket/SSE message", error);
    return null;
  }
}
