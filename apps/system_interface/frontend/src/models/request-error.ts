const UNREACHABLE_HTTP_CODES = new Set([0, 502, 503, 504]);

/**
 * True when `message` is the stringified-nullish artifact mithril produces for a
 * non-JSON error body. Mithril sets `xhr.responseType = "json"`, so a plain-text
 * proxy error (e.g. a 502/503/504 from the front-door proxy when the backend is
 * down) leaves `xhr.response` null; reading `xhr.responseText` then throws, and
 * mithril falls back to `new Error(xhr.response)`, i.e. `new Error(null)`, whose
 * `.message` is the literal 4-character string `"null"` (or `"undefined"`). It is
 * never a real application error message, so we treat it as a signal, not text.
 */
function isNullArtifactMessage(message: unknown): boolean {
  if (typeof message !== "string") {
    return false;
  }
  const trimmed = message.trim();
  return trimmed === "null" || trimmed === "undefined";
}

/**
 * Extract a human-readable message from a failed `m.request` rejection.
 *
 * Mithril rejects with an Error-like value carrying (when available) the parsed
 * response body, an HTTP status `code`, and a `message`. A gateway error (e.g. a
 * 504 from a front-door proxy) often has no JSON body, so naive
 * `response.detail` extraction yields `null`/`undefined` and surfaces a useless
 * "null" to the user. This walks the available fields in order of usefulness and
 * always returns a non-empty string. The literal `"null"`/`"undefined"` artifact
 * (see `isNullArtifactMessage`) is rejected in the message branch so it falls
 * through to the HTTP-code fallback rather than being shown verbatim.
 */
export function describeRequestError(error: unknown): string {
  if (error === null || error === undefined) {
    return "unknown error";
  }
  if (typeof error === "string") {
    return error.trim() || "unknown error";
  }
  const err = error as { response?: { detail?: unknown } | null; message?: unknown; code?: unknown };

  const detail = err.response?.detail;
  if (typeof detail === "string" && detail.trim() !== "") {
    return detail.trim();
  }

  const message = err.message;
  if (typeof message === "string") {
    const trimmed = message.trim();
    if (trimmed !== "" && !isNullArtifactMessage(trimmed)) {
      return trimmed;
    }
  }

  if (typeof err.code === "number" && err.code !== 0) {
    return `request failed (HTTP ${err.code})`;
  }

  return "unknown error";
}

/**
 * True when the failure is a transient "backend unreachable" condition (the
 * container/backend is down or the XHR never completed) rather than a real
 * application error. Callers use this to decide whether to hold the request and
 * reconnect+retry (unreachable) versus alert the user (application error).
 *
 * A code of 0 means the XHR never completed (network failure); 502/503/504 are
 * front-door proxy errors emitted when the backend is unreachable. The
 * `"null"`/`"undefined"` message artifact is a strong secondary signal of a
 * non-JSON proxy error body, which only happens for a dead/unreachable backend.
 */
export function isBackendUnreachableError(error: unknown): boolean {
  if (error === null || error === undefined || typeof error === "string") {
    return false;
  }
  const err = error as { message?: unknown; code?: unknown };

  if (typeof err.code === "number" && UNREACHABLE_HTTP_CODES.has(err.code)) {
    return true;
  }

  return isNullArtifactMessage(err.message);
}
