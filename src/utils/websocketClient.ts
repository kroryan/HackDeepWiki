/**
 * WebSocket client for chat completions
 * This replaces the HTTP streaming endpoint with a WebSocket connection
 */

import { getBackendWebSocketUrl } from '@/utils/backendUrl';

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

export interface ChatCompletionRequest {
  repo_url: string;
  messages: ChatMessage[];
  retrieval_query?: string;
  filePath?: string;
  token?: string;
  type?: string;
  // Wiki page id (repo chat) or ZIM entry path (type: 'zim') the chat was
  // opened from. When set, the backend scopes the initial context to that
  // page/entry plus a handful of related ones instead of the whole
  // repo/archive -- important for .zim archives, which can hold millions
  // of entries.
  current_page_id?: string;
  provider?: string;
  model?: string;
  language?: string;
  excluded_dirs?: string;
  excluded_files?: string;
  api_key?: string;
  api_endpoint?: string;
  // 🔐 When true, the latest saved Security Analysis / Website Security scan
  // report for owner/repo is summarized and injected into the prompt.
  include_security_context?: boolean;
  owner?: string;
  repo?: string;
  // Engraphis memory scope anchor: the wiki release the chat is opened on.
  // Memory is hard-isolated per wiki release (shared with the code editor
  // for that same release), so the backend needs to know which one this is.
  wiki_version?: number;
}

/**
 * Creates a WebSocket connection for chat completions
 * @param request The chat completion request
 * @param onMessage Callback for received messages
 * @param onError Callback for errors
 * @param onClose Callback for when the connection closes
 * @returns The WebSocket connection
 */
export const createChatWebSocket = (
  request: ChatCompletionRequest,
  onMessage: (message: string) => void,
  onError: (error: Event) => void,
  onClose: () => void
): Promise<WebSocket> => {
  // Create WebSocket connection
  return getBackendWebSocketUrl('/ws/chat').then((url) => {
    const ws = new WebSocket(url);

    // Set up event handlers
    ws.onopen = () => {
      console.log('WebSocket connection established');
      ws.send(JSON.stringify(request));
    };

    ws.onmessage = (event) => {
      onMessage(event.data);
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      onError(error);
    };

    ws.onclose = () => {
      console.log('WebSocket connection closed');
      onClose();
    };

    return ws;
  });
};

/**
 * Closes a WebSocket connection
 * @param ws The WebSocket connection to close
 */
export const closeWebSocket = (ws: WebSocket | null): void => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.close();
  }
};
