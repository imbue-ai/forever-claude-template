/**
 * Confirmation dialog for destroying an agent.
 *
 * Stays mounted across the destroy request so a failure can be surfaced
 * inline (instead of a blocking alert()): while `busy` the actions show a
 * "Destroying..." state, and on failure `error` is rendered in the dialog with
 * the buttons restored so the user can retry or cancel.
 */

import m from "mithril";

interface DestroyConfirmDialogAttrs {
  agentName: string;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

export const DestroyConfirmDialog: m.Component<DestroyConfirmDialogAttrs> = {
  view(vnode) {
    const { agentName, busy, error, onConfirm, onCancel } = vnode.attrs;

    return m(
      "div.destroy-dialog-overlay",
      {
        onclick: (e: Event) => {
          if (e.target === e.currentTarget && !busy) onCancel();
        },
      },
      [
        m("div.destroy-dialog", [
          m("h3.destroy-dialog-title", "Destroy Agent"),
          m("p.destroy-dialog-message", [
            `Are you sure you want to destroy `,
            m("strong", agentName),
            `? This cannot be undone.`,
          ]),
          error ? m("p.destroy-dialog-error", { style: "color: #dc2626;" }, error) : null,
          m("div.destroy-dialog-actions", [
            m("button.destroy-dialog-btn.destroy-dialog-btn-cancel", { onclick: onCancel, disabled: busy }, "Cancel"),
            m(
              "button.destroy-dialog-btn.destroy-dialog-btn-destroy",
              { onclick: onConfirm, disabled: busy },
              busy ? "Destroying..." : "Destroy",
            ),
          ]),
        ]),
      ],
    );
  },
};
