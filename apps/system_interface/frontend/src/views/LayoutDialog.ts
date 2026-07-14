/**
 * Shared dialog for the three "+"-menu layout actions: Save / Load / Delete.
 *
 * All three modes show the list of named layouts with the client's active
 * layout marked "(current)". Save additionally has a free-text name field
 * (prefilled with the active layout's name, so save-over-current is one
 * click); clicking a listed layout fills the field. Load and Delete are
 * pick-then-confirm.
 */

import m from "mithril";
import type { LayoutInfo } from "../models/WorkspaceLayouts";

export type LayoutDialogMode = "save" | "load" | "delete";

interface LayoutDialogAttrs {
  mode: LayoutDialogMode;
  layouts: LayoutInfo[];
  activeSlug: string;
  // Save mode passes the typed display name; load/delete pass the selected slug.
  onConfirm: (value: string) => void;
  onCancel: () => void;
}

const TITLE_BY_MODE: Record<LayoutDialogMode, string> = {
  save: "Save layout",
  load: "Load layout",
  delete: "Delete layout",
};

const CONFIRM_LABEL_BY_MODE: Record<LayoutDialogMode, string> = {
  save: "Save",
  load: "Load",
  delete: "Delete",
};

export function LayoutDialog(): m.Component<LayoutDialogAttrs> {
  let nameInput = "";
  let selectedSlug: string | null = null;
  let isInitialized = false;

  function confirmValue(attrs: LayoutDialogAttrs): string | null {
    if (attrs.mode === "save") {
      const trimmed = nameInput.trim();
      return trimmed.length > 0 ? trimmed : null;
    }
    return selectedSlug;
  }

  function submit(attrs: LayoutDialogAttrs): void {
    const value = confirmValue(attrs);
    if (value === null) return;
    attrs.onConfirm(value);
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      if (!isInitialized) {
        isInitialized = true;
        const active = attrs.layouts.find((layout) => layout.slug === attrs.activeSlug);
        nameInput = active?.display_name ?? "";
        selectedSlug = attrs.mode === "load" || attrs.mode === "delete" ? (active?.slug ?? null) : null;
      }

      const listItems = attrs.layouts.map((layout) => {
        const isCurrent = layout.slug === attrs.activeSlug;
        const isSelected =
          attrs.mode === "save" ? layout.display_name === nameInput.trim() : layout.slug === selectedSlug;
        return m(
          "div.layout-dialog-item",
          {
            class: isSelected ? "layout-dialog-item-selected" : "",
            onclick() {
              if (attrs.mode === "save") {
                nameInput = layout.display_name;
              } else {
                selectedSlug = layout.slug;
              }
            },
          },
          `${layout.display_name}${isCurrent ? " (current)" : ""}`,
        );
      });

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
              m("h3.custom-url-dialog-title", TITLE_BY_MODE[attrs.mode]),
              m("div.layout-dialog-list", listItems),
              attrs.mode === "save"
                ? [
                    m("label.custom-url-dialog-label", "Layout name"),
                    m("input.custom-url-dialog-input", {
                      type: "text",
                      value: nameInput,
                      placeholder: "layout name",
                      autofocus: true,
                      oninput(e: InputEvent) {
                        nameInput = (e.target as HTMLInputElement).value;
                      },
                      onkeydown(e: KeyboardEvent) {
                        if (e.key === "Enter") submit(attrs);
                        if (e.key === "Escape") attrs.onCancel();
                      },
                    }),
                  ]
                : null,
              m("div.custom-url-dialog-actions", [
                m("button.custom-url-dialog-cancel", { onclick: attrs.onCancel }, "Cancel"),
                m(
                  "button.custom-url-dialog-open",
                  {
                    onclick: () => submit(attrs),
                    disabled: confirmValue(attrs) === null,
                  },
                  CONFIRM_LABEL_BY_MODE[attrs.mode],
                ),
              ]),
            ],
          ),
        ],
      );
    },
  };
}
