/**
 * Clickable "choice cards" rendered inline in the transcript.
 *
 * Assistant prose is sanitized through DOMPurify (see markdown.ts), so an agent
 * cannot emit live `<button onclick=...>` HTML. Instead it emits a fenced marker
 * block and the frontend substitutes real components for it -- the same
 * detect-a-marker-and-render-a-component shape as the permission card, but keyed
 * off the message *text* rather than a tool call. The marker is a code fence with
 * the info string `minds-choices` wrapping a JSON array of choices:
 *
 *     ```minds-choices
 *     [
 *       {"title": "Consolidate your messages", "subtitle": "...", "prefill": "Help me ..."},
 *       {"title": "Suggest a few things", "prefill": "Suggest a few things I could work on."}
 *     ]
 *     ```
 *
 * Clicking a card drops its `prefill` into the composer (via the InputDraft
 * store) and focuses it -- prefill only, never an automatic send -- so the user
 * can edit before sending. An empty `prefill` just focuses the empty box (for an
 * "I have something in mind, let me type it" option).
 *
 * Parsing is deliberately forgiving: a fence whose body is not a valid choices
 * array is left untouched in the markdown stream, so a malformed block degrades
 * to a visible code block rather than vanishing.
 */

import m from "mithril";
import { setInputDraft } from "../models/InputDraft";

export interface Choice {
  title: string;
  subtitle?: string;
  prefill: string;
}

export type MessageSegment = { kind: "markdown"; text: string } | { kind: "choices"; choices: Choice[] };

const FENCE = "```";
const CHOICES_INFO_STRING = "minds-choices";

/** Validate/parse the JSON body of a `minds-choices` fence into choices, or null
 *  if it isn't a non-empty array of `{title, prefill, subtitle?}` objects. */
export function parseChoicesJson(jsonBody: string): Choice[] | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(jsonBody);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed) || parsed.length === 0) {
    return null;
  }
  const choices: Choice[] = [];
  for (const item of parsed) {
    if (typeof item !== "object" || item === null) {
      return null;
    }
    const obj = item as Record<string, unknown>;
    if (typeof obj.title !== "string" || typeof obj.prefill !== "string") {
      return null;
    }
    const choice: Choice = { title: obj.title, prefill: obj.prefill };
    if (typeof obj.subtitle === "string") {
      choice.subtitle = obj.subtitle;
    }
    choices.push(choice);
  }
  return choices;
}

/**
 * Split assistant text into an ordered run of markdown and choices segments.
 * Markdown runs render through the existing MarkdownContent; choices segments
 * render as cards. A message with no marker yields a single markdown segment, so
 * callers can treat the no-cards case uniformly.
 */
export function parseChoiceSegments(text: string): MessageSegment[] {
  const lines = text.split("\n");
  const segments: MessageSegment[] = [];
  let markdownBuffer: string[] = [];

  function flushMarkdown(): void {
    // Drop the blank lines that separate prose from a cards block at the segment
    // edges (only whole blank lines at the start/end, so internal content -- e.g.
    // an indented code block -- is preserved untouched). A run that is entirely
    // blank yields no segment.
    let start = 0;
    let end = markdownBuffer.length;
    while (start < end && markdownBuffer[start].trim() === "") {
      start++;
    }
    while (end > start && markdownBuffer[end - 1].trim() === "") {
      end--;
    }
    if (end > start) {
      segments.push({ kind: "markdown", text: markdownBuffer.slice(start, end).join("\n") });
    }
    markdownBuffer = [];
  }

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim() === FENCE + CHOICES_INFO_STRING) {
      // Scan for the closing fence.
      let close = i + 1;
      while (close < lines.length && lines[close].trim() !== FENCE) {
        close++;
      }
      if (close < lines.length) {
        const choices = parseChoicesJson(lines.slice(i + 1, close).join("\n"));
        if (choices !== null) {
          flushMarkdown();
          segments.push({ kind: "choices", choices });
          i = close + 1;
          continue;
        }
      }
      // No closing fence, or the body wasn't a valid choices array: treat the
      // opening fence line as ordinary markdown and keep scanning.
    }
    markdownBuffer.push(line);
    i++;
  }
  flushMarkdown();
  return segments;
}

/** Render a row of choice cards. Each card prefills the composer for `agentId`
 *  with its `prefill` text on click (focus handled by the composer). */
export function renderChoiceCards(choices: Choice[], agentId: string): m.Vnode {
  return m(
    "div",
    { class: "choice-cards" },
    choices.map((choice) =>
      m(
        "button",
        {
          class: "choice-card",
          type: "button",
          onclick(e: Event) {
            e.preventDefault();
            setInputDraft(agentId, choice.prefill);
          },
        },
        [
          m("span", { class: "choice-card-title" }, choice.title),
          choice.subtitle ? m("span", { class: "choice-card-subtitle" }, choice.subtitle) : null,
        ],
      ),
    ),
  );
}
