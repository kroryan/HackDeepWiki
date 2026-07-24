'use client';

/**
 * Live activity feed for the Code Editing right panel.
 *
 * Connects to WS /ws/code/events (normalized {t: ...} envelopes produced
 * server-side in api/code_agent/events.py::normalize_for_panel), accumulates
 * a bounded event list, and reconnects with backoff while the panel stays
 * mounted. `diffTick` bumps whenever the working tree likely changed, so the
 * diff view knows when to refetch without polling.
 */

import { useEffect, useRef, useState } from 'react';
import { getBackendWebSocketUrl } from '@/utils/backendUrl';
import { CodeSessionInfo } from '@/utils/codeAgentClient';

export interface CodeAgentEvent {
  t: string;
  [key: string]: unknown;
}

export type CodeAgentStatus =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'no_instance'
  | 'crashed'
  | 'closed';

const MAX_EVENTS = 500;

export function useCodeAgentEvents(session: CodeSessionInfo | null) {
  const [events, setEvents] = useState<CodeAgentEvent[]>([]);
  const [status, setStatus] = useState<CodeAgentStatus>('idle');
  const [diffTick, setDiffTick] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!session) {
      setEvents([]);
      setStatus('idle');
      return;
    }

    let cancelled = false;
    let retryDelay = 500;
    let retryTimer: number | undefined;

    const connect = async () => {
      if (cancelled) return;
      setStatus('connecting');
      try {
        const url = await getBackendWebSocketUrl('/ws/code/events');
        if (cancelled) return;
        const ws = new WebSocket(url);
        wsRef.current = ws;

        ws.onopen = () => {
          retryDelay = 500;
          ws.send(JSON.stringify({
            repo_key: session.repo_key,
            session_id: session.session_id,
          }));
        };

        ws.onmessage = (msg) => {
          let event: CodeAgentEvent;
          try {
            event = JSON.parse(msg.data);
          } catch {
            return;
          }
          if (event.t === 'status') {
            const state = String(event.state || '');
            if (state === 'connected') setStatus('connected');
            else if (state === 'no_instance') setStatus('no_instance');
            else if (state === 'crashed') setStatus('crashed');
          }
          if (event.t === 'diff_hint' || event.t === 'file_edited' || event.t === 'shell') {
            setDiffTick((tick) => tick + 1);
          }
          setEvents((prev) => {
            const next = [...prev, { ...event, _ts: Date.now() }];
            return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
          });
        };

        ws.onclose = () => {
          if (cancelled) return;
          setStatus((current) => (current === 'crashed' || current === 'no_instance' ? current : 'closed'));
          // Reconnect while mounted: a backend restart or an idle-reaped
          // instance shouldn't leave a dead panel. The resubscribe also
          // resyncs the diff view (diffTick bump on 'connected').
          retryTimer = window.setTimeout(connect, retryDelay);
          retryDelay = Math.min(retryDelay * 2, 8000);
        };

        ws.onerror = () => {
          ws.close();
        };
      } catch (error) {
        console.warn('Code agent events connection failed:', error);
        retryTimer = window.setTimeout(connect, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 8000);
      }
    };

    connect();
    return () => {
      cancelled = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      wsRef.current?.close();
      wsRef.current = null;
    };
    // Reconnect when the agent session identity changes.
  }, [session?.repo_key, session?.session_id]); // eslint-disable-line react-hooks/exhaustive-deps

  return { events, status, diffTick };
}
