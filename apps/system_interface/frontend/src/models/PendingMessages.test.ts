import { describe, expect, it, vi, beforeEach } from "vitest";
import type { TranscriptEvent, UserMessageEvent, AssistantMessageEvent } from "./Response";

// Mithril captures `requestAnimationFrame` at import time to schedule redraws;
// the node test env has no such global, so addPendingMessage's `m.redraw()`
// would throw without this polyfill (see Response.test.ts for the same dance).
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

// The activity state the mocked AgentManager reports for any agent. Mutated per
// test (and across the lifetime of a single send) to drive the idle/working
// branches of the forced-THINKING logic.
let mockActivityState: string | null = null;
// Captures the listener registered via addAgentActivityListener so a test can
// drive the working->IDLE safeguard through its real wiring (no test-only
// export of the internal handler). The agent-state manager owns transition
// detection, so the test emits explicit (previous, current) pairs.
let capturedActivityListener: ((agentId: string, previous: string | null, current: string | null) => void) | null =
  null;
vi.mock("./AgentManager", () => ({
  getAgentById: (id: string) => ({ id, activity_state: mockActivityState }),
  addAgentActivityListener: (listener: (agentId: string, previous: string | null, current: string | null) => void) => {
    capturedActivityListener = listener;
  },
}));

import {
  addPendingMessage,
  getPendingMessages,
  getPendingMessage,
  reconcilePendingMessages,
  removePendingMessage,
  markPendingMessageQueued,
  markPendingMessageSending,
  getEffectiveActivityState,
  initQueuedMessageIdleClearing,
} from "./PendingMessages";

// Register the safeguard once and drive it via the captured listener. Calling a
// real registration path (rather than exporting the internal handler) keeps the
// wiring under test.
initQueuedMessageIdleClearing();
function emitActivityTransition(agentId: string, previous: string | null, current: string | null): void {
  if (capturedActivityListener === null) {
    throw new Error("agent-activity listener was not registered");
  }
  capturedActivityListener(agentId, previous, current);
}

function userMsg(id: string, content: string): UserMessageEvent {
  return {
    type: "user_message",
    event_id: id,
    source: "claude/common_transcript",
    role: "user",
    content,
    timestamp: "2026-01-01T00:00:00Z",
  };
}

function assistantMsg(id: string, text: string): AssistantMessageEvent {
  return {
    type: "assistant_message",
    event_id: id,
    source: "claude/common_transcript",
    model: "m",
    text,
    tool_calls: [],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
    timestamp: "2026-01-01T00:00:00Z",
  };
}

// The module store persists across tests; a fresh agent id per test keeps them
// isolated without needing a reset hook.
let counter = 0;
function freshAgentId(): string {
  return `agent-${counter++}`;
}

beforeEach(() => {
  mockActivityState = "IDLE";
});

describe("optimistic message display", () => {
  it("shows a sent message immediately as a pending bubble", () => {
    const agentId = freshAgentId();
    addPendingMessage(agentId, "hello there", []);

    const pending = getPendingMessages(agentId);
    expect(pending).toHaveLength(1);
    expect(pending[0].content).toBe("hello there");
  });

  it("trims the message and ignores a blank send", () => {
    const agentId = freshAgentId();
    addPendingMessage(agentId, "  spaced  ", []);
    addPendingMessage(agentId, "   ", []);

    const pending = getPendingMessages(agentId);
    expect(pending).toHaveLength(1);
    expect(pending[0].content).toBe("spaced");
  });
});

describe("forced Thinking indicator", () => {
  it("forces THINKING when the agent was idle at send time", () => {
    const agentId = freshAgentId();
    mockActivityState = "IDLE";
    addPendingMessage(agentId, "do the thing", []);

    expect(getEffectiveActivityState(agentId)).toBe("THINKING");
  });

  it("leaves a working agent's real state untouched (does not force)", () => {
    const agentId = freshAgentId();
    mockActivityState = "TOOL_RUNNING";
    addPendingMessage(agentId, "sent mid-run", []);

    // Real work is shown as-is...
    expect(getEffectiveActivityState(agentId)).toBe("TOOL_RUNNING");

    // ...and even after the running turn settles to IDLE, a message that was
    // sent while working must NOT retroactively force THINKING.
    mockActivityState = "IDLE";
    expect(getEffectiveActivityState(agentId)).toBe("IDLE");
  });

  it("does not force THINKING for an agent with no activity tracking", () => {
    const agentId = freshAgentId();
    mockActivityState = null;
    addPendingMessage(agentId, "remote agent", []);

    expect(getEffectiveActivityState(agentId)).toBeNull();
  });

  it("stops forcing THINKING once the message reconciles", () => {
    const agentId = freshAgentId();
    mockActivityState = "IDLE";
    addPendingMessage(agentId, "hi", []);
    expect(getEffectiveActivityState(agentId)).toBe("THINKING");

    // The real transcript event lands; the agent is (still) idle afterwards.
    reconcilePendingMessages(agentId, [userMsg("u1", "hi")]);
    expect(getPendingMessages(agentId)).toHaveLength(0);
    expect(getEffectiveActivityState(agentId)).toBe("IDLE");
  });
});

describe("reconciliation against the transcript", () => {
  it("drops the bubble when its real event arrives", () => {
    const agentId = freshAgentId();
    addPendingMessage(agentId, "fix the bug", []);

    reconcilePendingMessages(agentId, [assistantMsg("a1", "ok"), userMsg("u1", "fix the bug")]);

    expect(getPendingMessages(agentId)).toHaveLength(0);
  });

  it("keeps a mid-run message visible until its real event finally lands", () => {
    const agentId = freshAgentId();
    mockActivityState = "TOOL_RUNNING";
    addPendingMessage(agentId, "queued while running", []);

    // The running turn keeps producing output; the queued message is not in the
    // transcript yet, so the bubble must persist.
    reconcilePendingMessages(agentId, [assistantMsg("a1", "still working"), assistantMsg("a2", "more work")]);
    expect(getPendingMessages(agentId)).toHaveLength(1);

    // The turn finally ends and Claude writes the queued message; now it reconciles.
    reconcilePendingMessages(agentId, [
      assistantMsg("a1", "still working"),
      assistantMsg("a2", "more work"),
      userMsg("u1", "queued while running"),
    ]);
    expect(getPendingMessages(agentId)).toHaveLength(0);
  });

  it("does not reconcile against an identical message that predates the send", () => {
    const agentId = freshAgentId();
    // The transcript already contains an identical earlier turn.
    const priorEvents: TranscriptEvent[] = [userMsg("old", "ship it")];
    addPendingMessage(agentId, "ship it", priorEvents);

    // A reconcile pass that still only sees the old message must not claim it.
    reconcilePendingMessages(agentId, priorEvents);
    expect(getPendingMessages(agentId)).toHaveLength(1);

    // Once the genuinely new event appears, the bubble reconciles.
    reconcilePendingMessages(agentId, [userMsg("old", "ship it"), userMsg("new", "ship it")]);
    expect(getPendingMessages(agentId)).toHaveLength(0);
  });

  it("reconciles a slash command whose typed args were newline-separated", () => {
    const agentId = freshAgentId();
    // The user typed the command, a newline, then a multi-line body. The parser
    // rebuilds the slash-command expansion as "/name args" joined by a single
    // space, so the transcript content differs from the typed text only in
    // whitespace; whitespace-normalized matching must still reconcile them.
    addPendingMessage(agentId, "/sculptor:sculpt-cli\nrun through the steps", []);

    reconcilePendingMessages(agentId, [userMsg("u1", "/sculptor:sculpt-cli run through the steps")]);

    expect(getPendingMessages(agentId)).toHaveLength(0);
  });

  it("matches two identical sends to two distinct transcript events", () => {
    const agentId = freshAgentId();
    addPendingMessage(agentId, "again", []);
    addPendingMessage(agentId, "again", []);
    expect(getPendingMessages(agentId)).toHaveLength(2);

    // Only one matching event so far: exactly one bubble should remain.
    reconcilePendingMessages(agentId, [userMsg("u1", "again")]);
    expect(getPendingMessages(agentId)).toHaveLength(1);

    // The second event lands: both are now reconciled.
    reconcilePendingMessages(agentId, [userMsg("u1", "again"), userMsg("u2", "again")]);
    expect(getPendingMessages(agentId)).toHaveLength(0);
  });
});

describe("rolling back a failed send", () => {
  it("drops the bubble and clears the forced THINKING override on removal", () => {
    const agentId = freshAgentId();
    mockActivityState = "IDLE";
    const id = addPendingMessage(agentId, "never delivered", []);
    expect(id).not.toBeNull();
    // The optimistic bubble is up and forcing THINKING while the send is in flight.
    expect(getPendingMessages(agentId)).toHaveLength(1);
    expect(getEffectiveActivityState(agentId)).toBe("THINKING");

    // The send fails: rolling the message back must clear both the bubble and
    // the override so the idle agent no longer looks busy forever.
    removePendingMessage(agentId, id as string);
    expect(getPendingMessages(agentId)).toHaveLength(0);
    expect(getEffectiveActivityState(agentId)).toBe("IDLE");
  });

  it("removes only the named message when identical sends are pending", () => {
    const agentId = freshAgentId();
    const first = addPendingMessage(agentId, "again", []);
    addPendingMessage(agentId, "again", []);
    expect(getPendingMessages(agentId)).toHaveLength(2);

    removePendingMessage(agentId, first as string);

    const remaining = getPendingMessages(agentId);
    expect(remaining).toHaveLength(1);
    expect(remaining[0].id).not.toBe(first);
  });

  it("is a no-op for an unknown id", () => {
    const agentId = freshAgentId();
    addPendingMessage(agentId, "still here", []);
    removePendingMessage(agentId, "pending-does-not-exist");
    expect(getPendingMessages(agentId)).toHaveLength(1);
  });
});

describe("lifecycle status", () => {
  it("starts a sent message in the sending state", () => {
    const agentId = freshAgentId();
    addPendingMessage(agentId, "hello", []);
    expect(getPendingMessages(agentId)[0].status).toBe("sending");
  });

  it("flips to queued when the send request resolves", () => {
    const agentId = freshAgentId();
    const id = addPendingMessage(agentId, "hello", []);
    markPendingMessageQueued(agentId, id as string);
    expect(getPendingMessages(agentId)[0].status).toBe("queued");
  });

  it("marks only the named message queued among identical sends", () => {
    const agentId = freshAgentId();
    const first = addPendingMessage(agentId, "dup", []);
    addPendingMessage(agentId, "dup", []);

    markPendingMessageQueued(agentId, first as string);

    const pending = getPendingMessages(agentId);
    expect(pending.find((p) => p.id === first)?.status).toBe("queued");
    expect(pending.find((p) => p.id !== first)?.status).toBe("sending");
  });

  it("can be put back into sending (for a re-send) and getPendingMessage reads it", () => {
    const agentId = freshAgentId();
    const id = addPendingMessage(agentId, "resend me", []) as string;
    markPendingMessageQueued(agentId, id);
    expect(getPendingMessage(agentId, id)?.status).toBe("queued");

    // "Interrupt and send" re-sends, so the message goes back to sending.
    markPendingMessageSending(agentId, id);
    expect(getPendingMessage(agentId, id)?.status).toBe("sending");
  });

  it("is a no-op for an unknown id", () => {
    const agentId = freshAgentId();
    addPendingMessage(agentId, "hello", []);
    markPendingMessageQueued(agentId, "pending-does-not-exist");
    expect(getPendingMessages(agentId)[0].status).toBe("sending");
  });

  it("keeps showing the bubble until reconciliation, even once queued", () => {
    const agentId = freshAgentId();
    const id = addPendingMessage(agentId, "queued while running", []);
    markPendingMessageQueued(agentId, id as string);

    // Queued, but the real transcript event has not arrived yet -- the bubble
    // must stay up (this is the mid-run case where the queued message is only
    // written to the transcript once the agent dequeues it).
    expect(getPendingMessages(agentId)).toHaveLength(1);

    reconcilePendingMessages(agentId, [userMsg("u1", "queued while running")]);
    expect(getPendingMessages(agentId)).toHaveLength(0);
  });
});

describe("clearing a stuck queued bubble when the agent goes idle", () => {
  // Queue a message sent to a working agent: it starts "sending", then resolves
  // to "queued" once the backend confirms it was accepted into the queue.
  function queueWhileWorking(agentId: string, content: string): string {
    mockActivityState = "THINKING";
    const id = addPendingMessage(agentId, content, []) as string;
    markPendingMessageQueued(agentId, id);
    return id;
  }

  it("drops a queued bubble that can never reconcile once the agent returns to idle", () => {
    const agentId = freshAgentId();
    // The user queued "deploy the staging build" but edited it in the terminal
    // before submitting, so the real transcript event carries different text and
    // never content-matches the bubble.
    queueWhileWorking(agentId, "deploy the staging build");
    reconcilePendingMessages(agentId, [userMsg("u1", "deploy the prod build")]);
    expect(getPendingMessages(agentId)).toHaveLength(1);

    // The agent finishes the (edited) turn and goes idle -- the backstop now
    // clears the orphaned bubble instead of leaving it up forever.
    emitActivityTransition(agentId, "THINKING", "IDLE");
    expect(getPendingMessages(agentId)).toHaveLength(0);
  });

  it("clears a queued bubble whose message was dropped by an agent restart", () => {
    const agentId = freshAgentId();
    // No transcript event ever arrives (the restart dropped the queue); the
    // restart drives activity from working to IDLE.
    queueWhileWorking(agentId, "summarize the diff");
    emitActivityTransition(agentId, "TOOL_RUNNING", "IDLE");
    expect(getPendingMessages(agentId)).toHaveLength(0);
  });

  it("does not clear a fresh send to an already-idle agent (no working->IDLE transition)", () => {
    const agentId = freshAgentId();
    mockActivityState = "IDLE";
    // The agent was already idle; sending to it briefly leaves a queued bubble
    // before it flips to THINKING. The only transition that fires here is the
    // agent appearing/becoming idle without first working (previous is null, or
    // it goes idle->thinking) -- never a working->IDLE -- so it must not clear.
    const id = addPendingMessage(agentId, "hi there", []) as string;
    markPendingMessageQueued(agentId, id);
    emitActivityTransition(agentId, null, "IDLE");
    expect(getPendingMessages(agentId)).toHaveLength(1);
  });

  it("leaves a still-sending message alone (protects interrupt-and-send)", () => {
    const agentId = freshAgentId();
    // "interrupt and send" marks the message back to sending, then interrupts --
    // which produces a transient working->IDLE. A sending (not queued) message
    // must survive that transition so the in-flight resend is not clobbered.
    mockActivityState = "THINKING";
    addPendingMessage(agentId, "resend me", []);
    emitActivityTransition(agentId, "THINKING", "IDLE");
    expect(getPendingMessages(agentId)).toHaveLength(1);
    expect(getPendingMessages(agentId)[0].status).toBe("sending");
  });

  it("does not clear while the agent merely moves between working states", () => {
    const agentId = freshAgentId();
    queueWhileWorking(agentId, "keep going");
    emitActivityTransition(agentId, "THINKING", "TOOL_RUNNING");
    expect(getPendingMessages(agentId)).toHaveLength(1);
  });
});
