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
// The Debug feed gets every bus event (text/thinking deltas included), so it
// churns much faster than the curated activity feed -- separate buffer.
const MAX_DEBUG_EVENTS = 1000;

export function useCodeAgentEvents(session: CodeSessionInfo | null) {
  const [events, setEvents] = useState<CodeAgentEvent[]>([]);
  const [debugEvents, setDebugEvents] = useState<CodeAgentEvent[]>([]);
  const [status, setStatus] = useState<CodeAgentStatus>('idle');
  // True while the agent session is actually doing something (opencode
  // session.status busy/idle) -- drives the Stop button and working chip.
  const [working, setWorking] = useState(false);
  const [diffTick, setDiffTick] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!session) {
      setEvents([]);
      setDebugEvents([]);
      setStatus('idle');
      setWorking(false);
      return;
    }

    let cancelled = false;
    // Backoff starts at 1.5s and only resets after a message actually
    // ARRIVES -- resetting on open (as the first version did) meant a
    // connection that dies right after opening reconnected at full speed
    // forever: the accept-storm seen in real-world logs.
    let retryDelay = 1500;
    let retryTimer: number | undefined;
    // Incoming events are BUFFERED and flushed to React state on a timer:
    // one setState per event meant one re-render per event, and during a
    // streaming answer that saturated the main thread badly enough that the
    // tab couldn't service its own WebSockets.
    const pending: { events: CodeAgentEvent[]; debug: CodeAgentEvent[]; diffHints: number } = {
      events: [], debug: [], diffHints: 0,
    };
    const flushTimer = window.setInterval(() => {
      if (pending.events.length > 0) {
        const batch = pending.events.splice(0);
        setEvents((prev) => {
          const next = [...prev, ...batch];
          return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
        });
      }
      if (pending.debug.length > 0) {
        const batch = pending.debug.splice(0);
        setDebugEvents((prev) => {
          const next = [...prev, ...batch];
          return next.length > MAX_DEBUG_EVENTS ? next.slice(next.length - MAX_DEBUG_EVENTS) : next;
        });
      }
      if (pending.diffHints > 0) {
        pending.diffHints = 0;
        setDiffTick((tick) => tick + 1);
      }
    }, 250);
    // The socket of THIS effect run. Tracked locally (not only via wsRef)
    // so the cleanup always closes the exact socket it created -- if the
    // effect re-runs while getBackendWebSocketUrl() is still in flight,
    // the old run's socket would otherwise open post-cleanup and live on
    // as an untracked zombie, each one spawning its own reconnect loop.
    let ws: WebSocket | null = null;

    const connect = async () => {
      if (cancelled) return;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return; // never run two connections from one effect run
      }
      setStatus('connecting');
      try {
        const url = await getBackendWebSocketUrl('/ws/code/events');
        if (cancelled) return;
        ws = new WebSocket(url);
        wsRef.current = ws;

        ws.onopen = () => {
          if (cancelled) {
            ws?.close();
            return;
          }
          ws?.send(JSON.stringify({
            repo_key: session.repo_key,
            session_id: session.session_id,
          }));
        };

        ws.onmessage = (msg) => {
          retryDelay = 1500; // a live, talking connection earns a fresh backoff
          let event: CodeAgentEvent;
          try {
            event = JSON.parse(msg.data);
          } catch {
            return;
          }
          if (event.t === 'status') {
            // Rare; applied immediately so the header dot stays honest.
            const state = String(event.state || '');
            if (state === 'connected') setStatus('connected');
            else if (state === 'no_instance') setStatus('no_instance');
            else if (state === 'crashed') { setStatus('crashed'); setWorking(false); }
            else if (state === 'busy') setWorking(true);
            else if (state === 'idle') setWorking(false);
          }
          if (event.t === 'diff_hint' || event.t === 'file_edited' || event.t === 'shell') {
            pending.diffHints += 1;
          }
          if (event.t === 'debug') {
            pending.debug.push({ ...event, _ts: Date.now() });
            return;
          }
          pending.events.push({ ...event, _ts: Date.now() });
        };

        ws.onclose = (event) => {
          if (cancelled) return;
          // Close code/reason in the console: if reconnects ever loop
          // again, this line says who hung up and why.
          console.warn(`Code agent events ws closed (code ${event.code}${event.reason ? `, ${event.reason}` : ''}); retrying in ${retryDelay}ms`);
          setStatus((current) => (current === 'crashed' || current === 'no_instance' ? current : 'closed'));
          // Reconnect while mounted: a backend restart or an idle-reaped
          // instance shouldn't leave a dead panel. The resubscribe also
          // resyncs the diff view (diffTick bump on 'connected').
          retryTimer = window.setTimeout(connect, retryDelay);
          retryDelay = Math.min(retryDelay * 2, 15000);
        };

        ws.onerror = () => {
          ws?.close();
        };
      } catch (error) {
        console.warn('Code agent events connection failed:', error);
        retryTimer = window.setTimeout(connect, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 15000);
      }
    };

    connect();
    return () => {
      cancelled = true;
      window.clearInterval(flushTimer);
      if (retryTimer) window.clearTimeout(retryTimer);
      // Close THIS run's socket specifically (ws), not just whatever wsRef
      // happens to point at -- see the zombie-socket note above.
      try {
        ws?.close();
      } catch {
        /* already closed */
      }
      wsRef.current = null;
    };
    // Reconnect when the agent session identity changes.
  }, [session?.repo_key, session?.session_id]); // eslint-disable-line react-hooks/exhaustive-deps

  return { events, debugEvents, status, working, diffTick };
}
