/**
 * message-kinds.ts -- THE enumeration of how every `user_message` in an agent
 * transcript is displayed, and the single source of truth a NEW HARNESS reads to
 * know what it must produce.
 *
 * ---------------------------------------------------------------------------
 * Why this file exists
 * ---------------------------------------------------------------------------
 * SCOPE: this catalogues ONLY the `user_message` event channel -- the transcript
 * slot that is OVERLOADED (a genuine human turn AND framework/system injections
 * all share it), and so needs disambiguating. Assistant-side surfaces
 * (tool-call blocks, tk step nodes, sub-agent cards, permission cards, plain
 * prose) are NOT user_messages, are not overloaded, and render through their own
 * dedicated paths (see message-renderers.ts + the event types in Response.ts).
 * They are not "kinds" and are untouched by this module.
 *
 * The transcript stream reuses the `user_message` event for several things that
 * are NOT a human typing: a Stop hook firing, a skill being expanded, the browser
 * fleet telling a queued agent its browser is free, a background-task completion
 * notice, etc. Left undistinguished, each of those renders as a bare user bubble,
 * as if the person had said it.
 *
 * Classification used to be scattered across the grouping and rendering layers as
 * ad-hoc string predicates, and each layer independently decided placement AND
 * appearance -- so "does this open a new turn" and "does this look like a chip"
 * were tangled into one function and drifted apart. This module untangles them:
 *
 *   1. A `UserMessageKind` names ONE type of display (not one source). Several
 *      sources can map to the same kind -- a Stop hook, a fleet notice, and a
 *      task notification are all the SAME display (a collapsed system chip) and
 *      so are all `UserMessageKind.SystemChip`, differing only by their chip label.
 *   2. `KIND_SPEC` records, per kind, exactly how it renders: which rail, whether
 *      it opens a new turn, and a prose description of the net visual. Read it to
 *      answer "what will my message look like?" without tracing render code.
 *
 * ---------------------------------------------------------------------------
 * Adding a harness (Codex, etc.)
 * ---------------------------------------------------------------------------
 * The `UserMessageKind`s below are harness-AGNOSTIC -- they are display buckets, not
 * Claude-specific markers. A new harness does NOT add kinds; it adds a detector
 * that maps ITS framework markers to these existing kinds (see
 * `classifyUserMessage` in message-classification.ts, which holds Claude Code's
 * detector table). At a glance, a harness author needs to answer, for each kind:
 * "does my framework emit a message that should display this way, and if so how
 * do I recognise it?" If a harness has no equivalent (e.g. no Stop hook), that
 * kind simply never occurs for it -- nothing downstream needs to change.
 *
 * Nothing in the grouping or rendering layers is harness-specific: they consume
 * `UserMessageKind` only.
 */

/** Which side / channel a message renders on. */
export enum Rail {
  /** Right-aligned "user" channel (`.message-user` / `.message-system-collapsed`). */
  User = "user",
  /** Left, full-width "assistant" channel -- used when a user_message's content
   *  is relocated into an assistant-side block (e.g. a skill body folded into its
   *  `Tool: Skill` call). */
  Assistant = "assistant",
  /** Not rendered as a row of its own. Either fully hidden, or its effect lands
   *  on some OTHER row (e.g. a permission verdict updates an earlier card). */
  None = "none",
}

/**
 * One kind per TYPE OF DISPLAY. See `KIND_SPEC` for the exact rendering of each.
 */
export enum UserMessageKind {
  /** A genuine human turn (also: a queued human prompt). */
  UserPrompt = "user-prompt",
  /**
   * A system/automated message injected into the transcript that IS worth
   * showing but is ugly raw -- a Stop hook, a browser-fleet nudge, a background
   * task-notification. Collapsed by default, expandable, on the user rail.
   */
  SystemChip = "system-chip",
  /** A skill expansion whose body is relocated into its `Tool: Skill` block. */
  SkillExpansion = "skill-expansion",
  /**
   * A message with no visual at all: the seeded `/welcome`, and -- via the
   * general `isMeta` rule in classifyUserMessage -- every framework-injected,
   * model-only message that no explicit detector surfaces (the resume-
   * continuation marker, the image coordinate note, MCP-resource dumps, hook
   * context, ...). One rule hides that whole family instead of a detector each.
   */
  Hidden = "hidden",
  /**
   * A permission-request verdict (granted/denied). Detected and handled
   * SEPARATELY from `classifyUserMessage` -- see `parsePermissionResolution` and
   * the dedicated branch in turn-grouping.ts -- because it not only opens a new
   * turn but also mutates an EARLIER permission card. Listed here so the display
   * catalogue is complete; `classifyUserMessage` never returns it.
   */
  PermissionResolution = "permission-resolution",
}

export interface KindSpec {
  /** Which rail / channel the message (or its relocated content) appears on. */
  rail: Rail;
  /**
   * Whether this message OPENS A NEW TURN SECTION (a boundary) or folds into the
   * current one. Independent of `rail` and of appearance: a `SystemChip` is on
   * the user rail yet is NOT a boundary (it tucks into the running turn as a
   * chip), exactly like a Stop hook.
   */
  boundary: boolean;
  /** Exact, human-readable description of the net visual -- the contract. */
  netVisual: string;
}

export const KIND_SPEC: Record<UserMessageKind, KindSpec> = {
  [UserMessageKind.UserPrompt]: {
    rail: Rail.User,
    boundary: true,
    netVisual:
      "Right-aligned rounded accent bubble opening a new turn section; the text " +
      "renders as light markdown. This is the baseline every other kind is " +
      "defined against.",
  },
  [UserMessageKind.SystemChip]: {
    rail: Rail.User,
    boundary: false,
    netVisual:
      "Right-aligned COLLAPSED chip ('▸ <label>') tucked INTO the current " +
      "turn -- it does NOT start a new turn. Click to expand the raw body. Same " +
      "chrome as Stop-hook feedback (`.message-system-collapsed` wrapping a " +
      "`.tool-call-block`). The chip label distinguishes the source " +
      "(e.g. 'Stop hook feedback', 'Browser fleet', 'Background task').",
  },
  [UserMessageKind.SkillExpansion]: {
    rail: Rail.Assistant,
    boundary: false,
    netVisual:
      "No row of its own on the user rail. Its SKILL.md body is relocated into " +
      "the preceding assistant-side 'Tool: Skill' tool-call block, where it shows " +
      "as that block's expandable output (see buildToolResultsWithSkillExpansions).",
  },
  [UserMessageKind.Hidden]: {
    rail: Rail.None,
    boundary: false,
    netVisual: "No DOM at all -- fully invisible (e.g. the seeded '/welcome').",
  },
  [UserMessageKind.PermissionResolution]: {
    rail: Rail.None,
    boundary: true,
    netVisual:
      "No row of its own. The extracted verdict (granted/denied/error) is written " +
      "onto the EARLIER permission-request card, and a fresh turn section opens " +
      "with no user bubble. Handled by parsePermissionResolution + turn-grouping, " +
      "not classifyUserMessage.",
  },
};

/**
 * Cross-layer contract: the sentinel the agentic browser fleet wraps its
 * agent-facing nudges in before sending them via `mngr message`, so this
 * frontend can recognise them (-> UserMessageKind.SystemChip) instead of showing a
 * bare user bubble. The fleet is the only sender; mngr itself is untouched (it is
 * an independent product and has no business knowing about this display concern).
 *
 * The wrapping side is `libs/browser/src/browser/session.py`
 * (`_SYSTEM_MESSAGE_TAG` in `_message_agent`). Keep the tag string in sync.
 * The tag adds no newlines, so a wrapped message types into the agent's pane
 * identically to the same text sent unwrapped.
 */
export const BROWSER_FLEET_TAG = "agentic-browser-fleet";
