/**
 * Modal dialog for creating a new browser in the per-workspace fleet.
 * Mirrors CreateAgentModal: a single "Browser Name" input pre-filled with a
 * random name that the user can edit.
 *
 * Browsers are addressed by NAME everywhere (not a numeric id): the CLI
 * ``<name>`` arg, the ``service:browser?session=<name>`` ref, the cast
 * WebSocket ``/browsers/<name>/cast``, the manifest, and the on-disk profile
 * dir all key off the chosen name. The user may type any valid name (lowercase
 * alnum words joined by single dashes); the daemon rejects invalid names (400)
 * and duplicates / a full fleet (409), and this modal surfaces the daemon's
 * error verbatim inline rather than alerting.
 *
 * Name validation (two layers, like the duplicate guard below). This modal
 * pre-validates the typed name against the SAME rule the daemon enforces
 * (``names.is_valid_browser_name``): lowercase alphanumeric words joined by
 * single dashes, 1..40 chars, no leading/trailing/double dash, not all-digits.
 * An invalid name shows an inline error and never opens a pane or POSTs, so the
 * user learns the rule immediately rather than watching an optimistic pane
 * appear then vanish. The daemon still validates authoritatively (its 400 is
 * re-surfaced via the re-opened modal below); this is just a fast, local guard.
 *
 * Duplicate-name guard (two layers): a typed name that already names a live
 * browser must NOT reach the optimistic-open path, because opening the pane for
 * an existing name would dedup onto that browser's pane and a subsequent 409
 * teardown would then close the EXISTING healthy pane. Layer one: this modal
 * pre-validates the typed name against ``existingBrowserNames`` and shows an
 * inline error without opening a pane or calling create. Layer two (defense in
 * depth, in the parent): ``onAccept`` reports whether it actually created a new
 * pane, and ``onFailed`` only tears the pane down when this flow created it.
 *
 * Close-immediately + optimistic 'starting' pane: the daemon now REGISTERS the
 * browser instantly (the Chromium launch runs serialized in the background and
 * the viewer watches it flip from ``init`` to ``running`` over the cast socket),
 * so the create POST returns fast. The instant the user confirms a non-empty,
 * non-duplicate name this modal:
 *   1. opens the optimistic pane via ``onAccept(name)`` (the viewer shows the full
 *      "Starting browser…" overlay until the daemon broadcasts ``running``), and
 *   2. CLOSES the modal immediately (the parent's ``onAccept`` clears the flag) --
 *      it does NOT wait for the POST.
 * The POST then runs in the background:
 *   - on success it calls ``onCreated(finalName)`` (the user always typed/accepted
 *     a name here, so it matches the already-open pane) to refresh the fleet list;
 *   - on failure (400 invalid / 409 duplicate-or-full / 503 installing / network)
 *     it calls ``onFailed(name, createdPane, reason)`` so the parent (1) tears down
 *     the optimistic pane (only when this flow created it) and (2) RE-OPENS this
 *     modal pre-filled with the typed name and the daemon's ``reason`` shown inline.
 *     The user must always learn WHY a browser didn't open -- a silently vanishing
 *     pane is not acceptable -- so the reason is carried out of the background POST
 *     and surfaced verbatim. ``reason`` is the daemon's ``{"error": ...}`` body for a
 *     400/409/503, or a generic network message when the POST never reached the
 *     daemon.
 *
 * Re-open pre-fill: the parent re-mounts the modal with ``initialName`` (the name
 * the user typed) and ``initialError`` (the daemon's reason). When ``initialName``
 * is set the modal does NOT fetch a fresh random name -- it keeps what the user
 * typed so they can edit just the offending part and retry.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

// Mirrors the daemon's ``names.is_valid_browser_name``: lowercase alphanumeric
// words joined by single dashes, 1..40 chars, no leading/trailing/double dash.
// Kept here (not the regex inline) so the rule reads the same as the Python one.
const BROWSER_NAME_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const MAX_BROWSER_NAME_LEN = 40;

// Validate a typed name against the daemon's rule. Returns ``null`` when valid,
// or a short inline error message explaining what is wrong. Pure-numeric names
// are rejected (the daemon rejects them too, so an upgraded workspace's old
// numeric profile dirs never resurrect as named browsers).
export function validateBrowserName(name: string): string | null {
  if (!name) {
    return "Enter a browser name.";
  }
  if (name.length > MAX_BROWSER_NAME_LEN) {
    return `Name must be at most ${MAX_BROWSER_NAME_LEN} characters.`;
  }
  if (/^[0-9]+$/.test(name)) {
    return "Name cannot be only digits.";
  }
  if (!BROWSER_NAME_RE.test(name)) {
    return "Use lowercase letters, numbers, and single dashes (e.g. alex-smith).";
  }
  return null;
}

interface CreateBrowserModalAttrs {
  // Service base URL for the browser daemon (``/service/browser/``). Passed in
  // so the modal does not need to import the workspace's service-URL helper.
  browserServiceUrl: string;
  // Names of the browsers already in the fleet (the same list that drives the
  // "active browser" dropdown). Used to pre-validate a typed name: a duplicate
  // is rejected inline before any pane is opened or any create is attempted.
  existingBrowserNames: string[];
  // When set, the modal is being RE-OPENED after a background create failed: it
  // pre-fills the input with this name (the one the user typed) instead of
  // fetching a fresh random name, so the user can fix and retry.
  initialName?: string;
  // The daemon's failure reason (or a network message) to show inline when the
  // modal is re-opened after a failed create. Paired with ``initialName``.
  initialError?: string | null;
  // Fired the instant the user accepts a non-empty name, BEFORE the POST
  // resolves, so the parent can open the optimistic 'starting' pane keyed by
  // this name. Returns ``true`` when a NEW pane was created, ``false`` when the
  // open deduped onto a pane that was already showing this browser -- the modal
  // forwards this to ``onFailed`` so a failure never closes a pre-existing pane.
  onAccept: (browserName: string) => boolean;
  // Fired after the create POST succeeds (the launch completed server-side).
  // Carries the daemon's final chosen name (equal to the accepted name, since
  // the user always supplies one here).
  onCreated: (browserName: string) => void;
  // Fired when the create POST fails (400 invalid / 409 duplicate-or-full /
  // 503 still installing / network). ``createdPane`` echoes the ``onAccept``
  // return so the parent only closes the optimistic pane when this flow actually
  // created it. ``reason`` is the daemon's error text (or a network message) so
  // the parent can re-open the modal pre-filled and surface WHY the create
  // failed -- the user must never be left with a pane that silently vanished.
  onFailed: (browserName: string, createdPane: boolean, reason: string) => void;
  onCancel: () => void;
}

export function CreateBrowserModal(): m.Component<CreateBrowserModalAttrs> {
  let name = "";
  let loading = false;
  let error: string | null = null;

  async function fetchRandomName(): Promise<void> {
    try {
      const response = await m.request<{ name: string }>({
        method: "GET",
        url: apiUrl("/api/random-name"),
      });
      name = response.name;
      m.redraw();
    } catch {
      name = `browser-${Date.now().toString(36)}`;
    }
  }

  async function submit(attrs: CreateBrowserModalAttrs): Promise<void> {
    const chosen = name.trim();
    if (!chosen || loading) {
      return;
    }

    // Layer one (pre-validate): if the typed name already names a live browser,
    // reject it inline. Crucially this happens BEFORE ``onAccept`` -- opening
    // the pane for an existing name would dedup onto that browser's healthy
    // pane, and the daemon's 409 would then tear it down. By stopping here we
    // never open a pane or call create for a duplicate. (The daemon still
    // enforces uniqueness authoritatively; this is just a fast, safe guard.)
    if (attrs.existingBrowserNames.includes(chosen)) {
      error = `A browser named ${chosen} already exists`;
      m.redraw();
      return;
    }

    // Pre-validate the NAME SYNTAX against the daemon's own rule, before opening a
    // pane or calling create. A bad name shows the inline error and stops here, so
    // the user learns the rule immediately rather than watching an optimistic pane
    // flash up and vanish on the daemon's 400. (The daemon still validates
    // authoritatively; its 400 is re-surfaced via onFailed if anything slips past.)
    const nameError = validateBrowserName(chosen);
    if (nameError !== null) {
      error = nameError;
      m.redraw();
      return;
    }

    loading = true;
    error = null;

    // Open the optimistic pane, then close the modal IMMEDIATELY -- we do not wait
    // for the POST. The pane shows the full "Starting browser…" overlay and flips to
    // the live page on its own when the daemon broadcasts ``running``. ``createdPane``
    // records whether this actually created a new pane so a later failure only closes
    // one this flow owns. ``onAccept`` (in the parent) also clears the modal flag.
    const createdPane = attrs.onAccept(chosen);

    // Background POST: registers the browser server-side (returns fast) and kicks off
    // the serialized launch. The modal is already gone, so success just refreshes the
    // fleet list; failure tears the optimistic pane back down AND re-opens this modal
    // pre-filled with the daemon's reason, so the user always learns why it failed.
    void (async () => {
      let response: globalThis.Response;
      try {
        response = await fetch(`${attrs.browserServiceUrl}browsers`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: chosen }),
        });
      } catch {
        attrs.onFailed(
          chosen,
          createdPane,
          "Could not reach the browser service. Check your connection and try again.",
        );
        return;
      }
      const data = (await response.json().catch(() => ({}))) as { name?: string; error?: string };
      if (response.ok) {
        attrs.onCreated(typeof data.name === "string" ? data.name : chosen);
        return;
      }
      // 400 invalid / 409 duplicate-or-full / 503 installing: the registration was
      // rejected, so tear down the optimistic pane (only if this flow created it) and
      // surface the daemon's reason verbatim (fallback to a generic line if absent).
      const reason =
        typeof data.error === "string" && data.error.trim() ? data.error : "The browser could not be created.";
      attrs.onFailed(chosen, createdPane, reason);
    })();
  }

  return {
    oninit(vnode) {
      // Re-opened after a failed create: keep the name the user typed and show the
      // daemon's reason inline, so they can fix and retry. Only fetch a fresh random
      // name on a clean open (no ``initialName``).
      if (typeof vnode.attrs.initialName === "string" && vnode.attrs.initialName) {
        name = vnode.attrs.initialName;
        error = vnode.attrs.initialError ?? null;
      } else {
        fetchRandomName();
      }
    },

    view(vnode) {
      const attrs = vnode.attrs;

      return m(
        "div.custom-url-dialog-overlay",
        {
          onclick(e: MouseEvent) {
            if ((e.target as HTMLElement).classList.contains("custom-url-dialog-overlay")) {
              attrs.onCancel();
            }
          },
        },
        [
          m(
            "div.custom-url-dialog",
            {
              onclick(e: MouseEvent) {
                e.stopPropagation();
              },
            },
            [
              m("h3.custom-url-dialog-title", "New browser"),
              m("label.custom-url-dialog-label", "Browser Name"),
              m("input.custom-url-dialog-input", {
                type: "text",
                value: name,
                placeholder: "browser-name",
                autofocus: true,
                oninput(e: InputEvent) {
                  name = (e.target as HTMLInputElement).value;
                },
                onkeydown(e: KeyboardEvent) {
                  if (e.key === "Enter") {
                    submit(attrs);
                  }
                  if (e.key === "Escape") {
                    attrs.onCancel();
                  }
                },
              }),
              error ? m("p", { style: "color: red; font-size: 0.85em; margin-top: 4px;" }, error) : null,
              m("div.custom-url-dialog-actions", [
                m(
                  "button.custom-url-dialog-cancel",
                  {
                    onclick: attrs.onCancel,
                    disabled: loading,
                  },
                  "Cancel",
                ),
                m(
                  "button.custom-url-dialog-open",
                  {
                    onclick: () => submit(attrs),
                    disabled: loading || !name.trim(),
                  },
                  loading ? "Starting..." : "Create",
                ),
              ]),
            ],
          ),
        ],
      );
    },
  };
}
