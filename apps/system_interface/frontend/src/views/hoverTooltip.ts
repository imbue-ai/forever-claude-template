/**
 * A hover tooltip that lives on ``document.body`` rather than next to its
 * target, positioned (fixed) just below the target on mouseenter.
 *
 * Tab content in dockview can use neither of the app's usual tooltip
 * mechanisms. Native ``title`` is suppressed: dockview marks every tab
 * ``draggable`` (tab.js sets ``element.draggable = true``, plus
 * ``-webkit-user-drag: element``), and Chromium hides ``title`` tooltips on
 * draggable elements and their descendants. A CSS ``::after`` bubble (the
 * ``data-tooltip`` pattern used elsewhere) is clipped by the tab strip's
 * overflow -- ``.dv-tabs-container`` is ``overflow: auto`` and ``.dv-groupview``
 * is ``overflow: hidden``. A body-level, fixed-position element driven by our
 * own mouseenter/mouseleave avoids both: it is not a native tooltip, and it is
 * not inside the clipping container.
 */
export interface HoverTooltip {
  /** Set the text shown on hover, or ``null`` to disable the tooltip. */
  setText(text: string | null): void;
  /** Remove listeners and any visible bubble. */
  dispose(): void;
}

export function attachHoverTooltip(target: HTMLElement): HoverTooltip {
  let text: string | null = null;
  let bubble: HTMLDivElement | null = null;

  const position = (): void => {
    if (!bubble) {
      return;
    }
    const rect = target.getBoundingClientRect();
    // Centred just below the target; the bubble's own transform pulls it back
    // by half its width (see ``.dv-tab-hover-tooltip``).
    bubble.style.top = `${rect.bottom + 6}px`;
    bubble.style.left = `${rect.left + rect.width / 2}px`;
  };

  const show = (): void => {
    if (!text) {
      return;
    }
    if (!bubble) {
      bubble = document.createElement("div");
      bubble.className = "dv-tab-hover-tooltip";
      document.body.appendChild(bubble);
    }
    bubble.textContent = text;
    position();
  };

  const hide = (): void => {
    bubble?.remove();
    bubble = null;
  };

  target.addEventListener("mouseenter", show);
  target.addEventListener("mouseleave", hide);

  return {
    setText(next: string | null): void {
      text = next;
      if (!text) {
        hide();
      } else if (bubble) {
        bubble.textContent = text;
        position();
      }
    },
    dispose(): void {
      target.removeEventListener("mouseenter", show);
      target.removeEventListener("mouseleave", hide);
      hide();
    },
  };
}
