/**
 * Client for Code Editing mode (the embedded opencode agent).
 *
 * ensureCodeSession() goes through the Next proxy route (quick JSON call);
 * the two WebSockets connect straight to the backend (WS can't traverse a
 * Next route handler), resolving the dynamic port the same way the normal
 * chat does (src/utils/backendUrl.ts).
 */

import { getBackendWebSocketUrl } from '@/utils/backendUrl';

export interface CodeSessionRequest {
  repo_url: string;
  type: string;
  owner: string;
  repo: string;
  provider: string;
  model?: string;
  api_key?: string;
  api_endpoint?: string;
  language?: string;
  wiki_version?: number;
  include_security_context?: boolean;
  existing_session_id?: string;
  title?: string;
}

export interface CodeSessionInfo {
  session_id: string;
  repo_key: string;
  repo_dir: string;
  is_local_type: boolean;
  opencode_version?: string | null;
  version_warning?: string | null;
  active_sessions: number;
  // "provider/model → endpoint" the agent talks to (connection-failure triage)
  model_target?: string | null;
}

export interface CodeChatRequest {
  repo_key: string;
  session_id: string;
  content: string;
  provider: string;
  model?: string;
  api_key?: string;
  api_endpoint?: string;
}

/** Typed error carrying the backend's {code, message} detail so the UI can
 * show the right i18n string (repo_not_cloned, opencode_unavailable, ...). */
export class CodeSessionError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

export const ensureCodeSession = async (
  request: CodeSessionRequest
): Promise<CodeSessionInfo> => {
  const response = await fetch('/api/code/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  if (!response.ok) {
    let code = 'unknown';
    let message = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      code = body?.detail?.code || code;
      message = body?.detail?.message || message;
    } catch {
      /* non-JSON error body */
    }
    throw new CodeSessionError(code, message);
  }
  return response.json();
};

/** Same contract as createChatWebSocket: the server streams plain answer
 * text plus sentinel-framed process events, so the caller reuses the exact
 * StreamParser/onMessage/onClose handlers of the normal chat. */
export const createCodeChatWebSocket = (
  request: CodeChatRequest,
  onMessage: (message: string) => void,
  onError: (error: Event) => void,
  onClose: () => void
): Promise<WebSocket> => {
  return getBackendWebSocketUrl('/ws/code/chat').then((url) => {
    const ws = new WebSocket(url);
    ws.onopen = () => {
      ws.send(JSON.stringify(request));
    };
    ws.onmessage = (event) => onMessage(event.data);
    ws.onerror = (error) => {
      console.error('Code chat WebSocket error:', error);
      onError(error);
    };
    ws.onclose = () => onClose();
    return ws;
  });
};

export const abortCodeSession = async (
  repoKey: string,
  sessionId: string
): Promise<void> => {
  try {
    await fetch('/api/code/abort', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_key: repoKey, session_id: sessionId }),
    });
  } catch (e) {
    console.warn('Code session abort failed:', e);
  }
};
