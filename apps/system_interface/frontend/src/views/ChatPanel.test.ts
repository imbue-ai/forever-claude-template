import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ChatPanel pulls in a wide module graph. Stub everything except the lifecycle
// behavior under test: the point of item A's refactor is that view() is pure
// (no connections/fetches) and all side effects fire from oninit/onupdate.

const { mockLoadSnapshotWithStream, mockConnectToStream, mockDisconnectFromStream } = vi.hoisted(() => ({
  mockLoadSnapshotWithStream: vi.fn(() => Promise.resolve()),
  mockConnectToStream: vi.fn(),
  mockDisconnectFromStream: vi.fn(),
}));

const { mockGetProtoAgents, mockGetEventsForAgent, mockIsConversationNotFound, mockIsBackfillComplete } = vi.hoisted(
  () => ({
    mockGetProtoAgents: vi.fn(() => [] as { agent_id: string }[]),
    mockGetEventsForAgent: vi.fn(() => [] as unknown[]),
    mockIsConversationNotFound: vi.fn(() => false),
    mockIsBackfillComplete: vi.fn(() => true),
  }),
);

// A minimal mithril stub: m() builds an inert vnode, and the static helpers are
// no-ops. The component code only needs these to construct its view tree.
vi.mock("mithril", () => ({
  default: Object.assign((tag: unknown, attrs?: unknown, children?: unknown) => ({ tag, attrs, children }), {
    redraw: () => {},
    request: () => Promise.resolve({}),
    trust: (s: string) => s,
  }),
}));

vi.mock("../slots", () => ({ isSlotClaimed: () => false }));
vi.mock("../models/Response", () => ({
  fetchBackfillEvents: vi.fn(() => Promise.resolve()),
  getEventsForAgent: mockGetEventsForAgent,
  getFirstEventId: () => null,
  isConversationNotFound: mockIsConversationNotFound,
  isBackfillComplete: mockIsBackfillComplete,
}));
vi.mock("../models/StreamingMessage", () => ({
  connectToStream: mockConnectToStream,
  disconnectFromStream: mockDisconnectFromStream,
  loadSnapshotWithStream: mockLoadSnapshotWithStream,
}));
vi.mock("../models/ws-json", () => ({ parseJsonMessage: (raw: string) => JSON.parse(raw) }));
vi.mock("../models/AgentManager", () => ({
  getAgentById: () => null,
  getProtoAgents: mockGetProtoAgents,
}));
vi.mock("../base-path", () => ({ apiUrl: (s: string) => s }));
vi.mock("./EmptySlot", () => ({ EmptySlot: {} }));
vi.mock("./MessageInput", () => ({ MessageInput: {} }));
vi.mock("./message-renderers", () => ({
  renderUserMessage: () => null,
  renderAssistantMessage: () => ({}),
  buildToolResultsMap: () => new Map(),
}));
vi.mock("./DockviewWorkspace", () => ({
  getTerminalUrl: () => "/service/terminal/",
  openIframeTabForAgent: vi.fn(),
}));

import { ChatPanel } from "./ChatPanel";

interface LifecycleComponent {
  oninit?: (vnode: { attrs: { agentId: string } }) => void;
  onupdate?: (vnode: { attrs: { agentId: string } }) => void;
  onremove?: () => void;
  view: (vnode: { attrs: { agentId: string } }) => unknown;
}

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }
  close(): void {}
}

let agentCounter = 0;

beforeEach(() => {
  FakeWebSocket.instances = [];
  globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
  globalThis.window = { location: { protocol: "http:", host: "localhost" } } as unknown as Window & typeof globalThis;
  mockLoadSnapshotWithStream.mockClear();
  mockConnectToStream.mockClear();
  mockDisconnectFromStream.mockClear();
  mockGetProtoAgents.mockReset();
  mockGetProtoAgents.mockReturnValue([]);
  mockIsConversationNotFound.mockReset();
  mockIsConversationNotFound.mockReturnValue(false);
  mockGetEventsForAgent.mockReset();
  mockGetEventsForAgent.mockReturnValue([]);
  mockIsBackfillComplete.mockReset();
  mockIsBackfillComplete.mockReturnValue(true);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ChatPanel lifecycle (item A: side effects out of view)", () => {
  it("kicks off the load from oninit and does not start it from view()", () => {
    const agentId = `agent-${agentCounter++}`;
    const component = ChatPanel() as unknown as LifecycleComponent;

    component.oninit?.({ attrs: { agentId } });
    expect(mockLoadSnapshotWithStream).toHaveBeenCalledTimes(1);
    expect(mockLoadSnapshotWithStream).toHaveBeenCalledWith(agentId);

    // Rendering must be pure: repeated view() calls trigger no new load.
    component.view({ attrs: { agentId } });
    component.view({ attrs: { agentId } });
    expect(mockLoadSnapshotWithStream).toHaveBeenCalledTimes(1);
  });

  it("does not re-load on subsequent onupdate for the same agent (idempotent guards)", () => {
    const agentId = `agent-${agentCounter++}`;
    const component = ChatPanel() as unknown as LifecycleComponent;

    component.oninit?.({ attrs: { agentId } });
    component.onupdate?.({ attrs: { agentId } });
    component.onupdate?.({ attrs: { agentId } });

    // ensureAgentLoaded's currentAgentId guard means the load happens once.
    expect(mockLoadSnapshotWithStream).toHaveBeenCalledTimes(1);
  });

  it("opens the proto build-log WebSocket from oninit, not from view()", () => {
    const agentId = `agent-${agentCounter++}`;
    mockGetProtoAgents.mockReturnValue([{ agent_id: agentId }]);
    const component = ChatPanel() as unknown as LifecycleComponent;

    component.oninit?.({ attrs: { agentId } });
    expect(FakeWebSocket.instances).toHaveLength(1);
    // No snapshot load while the agent is still a proto-agent.
    expect(mockLoadSnapshotWithStream).not.toHaveBeenCalled();

    // view() is pure: it does not open a second socket.
    component.view({ attrs: { agentId } });
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
