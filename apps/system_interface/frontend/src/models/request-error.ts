/**
 * Extract a human-readable message from a failed `m.request` rejection.
 *
 * Mithril rejects with an Error-like value carrying (when available) the parsed
 * response body, an HTTP status `code`, and a `message`. A gateway error (e.g. a
 * 504 from a front-door proxy) often has no JSON body, so naive
 * `response.detail` extraction yields `null`/`undefined` and surfaces a useless
 * "null" to the user. This walks the available fields in order of usefulness and
 * always returns a non-empty string.
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
  if (typeof message === "string" && message.trim() !== "") {
    return message.trim();
  }

  if (typeof err.code === "number" && err.code !== 0) {
    return `request failed (HTTP ${err.code})`;
  }

  return "unknown error";
}
