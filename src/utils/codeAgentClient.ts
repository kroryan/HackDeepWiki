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
  request: CodeSessionRequest,
  authorizationCode?: string
): Promise<CodeSessionInfo> => {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (authorizationCode) {
    headers['X-HackDeepWiki-Authorization'] = authorizationCode;
  }
  const response = await fetch('/api/code/session', {
    method: 'POST',
    headers,
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
  authorizationCode: string | undefined,
  onMessage: (message: string) => void,
  onError: (error: Event) => void,
  onClose: () => void | Promise<void>
): Promise<WebSocket> => {
  return getBackendWebSocketUrl('/ws/code/chat').then((url) => {
    const endpoint = new URL(url);
    if (authorizationCode) {
      endpoint.searchParams.set('authorization_code', authorizationCode);
    }
    const ws = new WebSocket(endpoint.toString());
    ws.onopen = () => {
      ws.send(JSON.stringify(request));
    };
    ws.onmessage = (event) => onMessage(event.data);
    ws.onerror = (error) => {
      console.error('Code chat WebSocket error:', error);
      onError(error);
    };
    ws.onclose = () => {
      void onClose();
    };
    return ws;
  });
};

export const abortCodeSession = async (
  repoKey: string,
  sessionId: string,
  authorizationCode?: string
): Promise<void> => {
  try {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (authorizationCode) {
      headers['X-HackDeepWiki-Authorization'] = authorizationCode;
    }
    await fetch('/api/code/abort', {
      method: 'POST',
      headers,
      body: JSON.stringify({ repo_key: repoKey, session_id: sessionId }),
    });
  } catch (e) {
    console.warn('Code session abort failed:', e);
  }
};

/** Read the durable OpenCode session shape defensively across OpenCode
 * releases. The endpoint returns messages as ``{info, parts}``; older
 * versions occasionally returned the role/content at the top level. */
export function extractLatestAssistantText(payload: unknown): string {
  if (!Array.isArray(payload)) return '';
  for (let index = payload.length - 1; index >= 0; index -= 1) {
    const message = payload[index] as Record<string, unknown>;
    const info = (message.info || {}) as Record<string, unknown>;
    if ((info.role || message.role) !== 'assistant') continue;
    if (typeof message.content === 'string') return message.content;
    const parts = Array.isArray(message.parts) ? message.parts : [];
    const text = parts
      .map((part) => {
        if (!part || typeof part !== 'object') return '';
        const item = part as Record<string, unknown>;
        return item.type === 'text' && typeof item.text === 'string' ? item.text : '';
      })
      .join('');
    if (text) return text;
  }
  return '';
}

export const getCodeSessionMessages = async (
  repoKey: string,
  sessionId: string,
  authorizationCode?: string
): Promise<unknown[]> => {
  const headers: Record<string, string> = {};
  if (authorizationCode) {
    headers['X-HackDeepWiki-Authorization'] = authorizationCode;
  }
  const params = new URLSearchParams({ repo_key: repoKey, session_id: sessionId });
  const response = await fetch(`/api/code/messages?${params.toString()}`, { headers });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  const body: unknown = await response.json();
  return Array.isArray(body) ? body : [];
};
