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
 * Duplicate-name guard (two layers): a typed name that already names a live
 * browser must NOT reach the optimistic-open path, because opening the pane for
 * an existing name would dedup onto that browser's pane and a subsequent 409
 * teardown would then close the EXISTING healthy pane. Layer one: this modal
 * pre-validates the typed name against ``existingBrowserNames`` and shows an
 * inline error without opening a pane or calling create. Layer two (defense in
 * depth, in the parent): ``onAccept`` reports whether it actually created a new
 * pane, and ``onFailed`` only tears the pane down when this flow created it.
 *
 * Optimistic 'starting' pane: the launch is serialized server-side (at most one
 * Chromium starts at a time), so a create can take several seconds -- longer
 * during restore. To make the tab appear IMMEDIATELY, this modal calls
 * ``onAccept(name)`` the instant the user confirms a non-empty name; the parent
 * opens the browser pane right then, which shows "Browser starting…" until the
 * cast WebSocket delivers the first frame (the viewer retries on the daemon's
 * 1013 "not registered yet" close). The POST then runs in the background:
 *   - on success the modal closes and calls ``onCreated(finalName)`` (the
 *     daemon may have substituted a name only when none was typed; here the
 *     user always typed/accepted one, so the names match and the already-open
 *     pane is correct);
 *   - on failure the modal stays open, shows the daemon's error inline, and
 *     calls ``onFailed(name)`` so the parent tears down the optimistic pane.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

interface CreateBrowserModalAttrs {
  // Service base URL for the browser daemon (``/service/browser/``). Passed in
  // so the modal does not need to import the workspace's service-URL helper.
  browserServiceUrl: string;
  // Names of the browsers already in the fleet (the same list that drives the
  // "active browser" dropdown). Used to pre-validate a typed name: a duplicate
  // is rejected inline before any pane is opened or any create is attempted.
  existingBrowserNames: string[];
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
  // 503 still installing). ``createdPane`` echoes the ``onAccept`` return so the
  // parent only closes the optimistic pane when this flow actually created it.
  onFailed: (browserName: string, createdPane: boolean) => void;
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

    loading = true;
    error = null;
    m.redraw();

    // Open the optimistic pane immediately, before the (serialized, possibly
    // slow) launch finishes. The pane shows "Browser starting…" and connects
    // once the daemon registers the name. ``createdPane`` records whether this
    // actually created a new pane so a failure only closes one this flow owns.
    const createdPane = attrs.onAccept(chosen);

    let response: globalThis.Response;
    try {
      response = await fetch(`${attrs.browserServiceUrl}browsers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: chosen }),
      });
    } catch (e) {
      // Network-level failure: tear down the optimistic pane and keep the modal
      // open with the error.
      attrs.onFailed(chosen, createdPane);
      error = (e as Error).message ?? "Creation failed";
      loading = false;
      m.redraw();
      return;
    }

    const data = (await response.json().catch(() => ({}))) as { name?: string; error?: string };
    if (response.ok) {
      // Launch completed. The pane was already opened on accept; just close
      // the modal and let the parent confirm/refresh.
      attrs.onCreated(typeof data.name === "string" ? data.name : chosen);
      return;
    }

    // 400 invalid name / 409 duplicate-or-full / 503 still installing: surface
    // the daemon's message verbatim, close the optimistic pane (only if this
    // flow created it), keep the modal open so the user can fix the name.
    attrs.onFailed(chosen, createdPane);
    error = typeof data.error === "string" ? data.error : `HTTP ${response.status}`;
    loading = false;
    m.redraw();
  }

  return {
    oninit() {
      fetchRandomName();
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
