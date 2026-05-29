/**
 * Agent discovery -- compatibility layer.
 * Delegates to AgentManager for state, kept for plugin/hook backward compatibility.
 */

import { getAgents as getAgentManagerAgents } from "./AgentManager";

// Keep Conversation interface for hook compatibility
export interface Conversation {
  id: string;
  name: string;
  model: string;
  latest_response_datetime_utc: string | null;
}

// Compatibility shim for hooks/slots that expect conversations
export function getConversations(): Conversation[] {
  return getAgentManagerAgents().map((a) => ({
    id: a.id,
    name: a.name,
    model: a.state,
    latest_response_datetime_utc: null,
  }));
}
