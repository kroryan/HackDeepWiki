'use client';

/**
 * Right-hand panel of the Code Editing split layout: live agent activity
 * (tool calls, shell commands with streamed output, file edits) and the
 * session's accumulated diffs. The user keeps chatting on the left; this
 * panel is where they *watch the agent work*.
 */

import React, { useEffect, useRef, useState } from 'react';
import {
  FaChevronRight,
  FaCircle,
  FaExclamationTriangle,
  FaStop,
  FaTerminal,
  FaFileAlt,
  FaWrench,
} from 'react-icons/fa';
import { useLanguage } from '@/contexts/LanguageContext';
import { CodeSessionInfo, abortCodeSession } from '@/utils/codeAgentClient';
import { useCodeAgentEvents, CodeAgentEvent, CodeAgentStatus } from '@/hooks/useCodeAgentEvents';
import CodeDiffView from './CodeDiffView';

interface CodeAgentPanelProps {
  session: CodeSessionInfo | null;
}

const STATUS_COLORS: Record<CodeAgentStatus, string> = {
  idle: 'text-[var(--muted)]',
  connecting: 'text-yellow-500',
  connected: 'text-green-500',
  no_instance: 'text-[var(--muted)]',
  crashed: 'text-red-500',
  closed: 'text-yellow-500',
};

function ActivityItem({ event }: { event: CodeAgentEvent }) {
  const [expanded, setExpanded] = useState(false);
  const t = event.t;

  if (t === 'shell') {
    const command = String(event.command || '');
    const output = String(event.output || '');
    return (
      <div className="rounded border border-[var(--border-color)]/50 overflow-hidden">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="w-full flex items-start gap-2 px-2 py-1.5 text-left hover:bg-[var(--background)]/50 transition-colors"
        >
          <FaTerminal className="mt-0.5 shrink-0 text-[var(--accent-primary)] text-[10px]" />
          <code className="text-[11px] font-mono truncate text-[var(--foreground)]">{command || '(shell)'}</code>
          <span className={`ml-auto shrink-0 text-[10px] ${event.status === 'error' ? 'text-red-500' : 'text-[var(--muted)]'}`}>
            {String(event.status || '')}
          </span>
          {output && <FaChevronRight className={`mt-0.5 h-2 w-2 shrink-0 text-[var(--muted)] transition-transform ${expanded ? 'rotate-90' : ''}`} />}
        </button>
        {expanded && output && (
          <pre className="px-2 py-1.5 text-[10px] font-mono whitespace-pre-wrap break-all bg-black/30 text-[var(--muted)] max-h-56 overflow-y-auto border-t border-[var(--border-color)]/40">
            {output}
          </pre>
        )}
      </div>
    );
  }

  if (t === 'file_edited') {
    return (
      <div className="flex items-center gap-2 px-2 py-1">
        <FaFileAlt className="shrink-0 text-[var(--accent-secondary)] text-[10px]" />
        <code className="text-[11px] font-mono truncate text-[var(--foreground)]">{String(event.file || '')}</code>
        <span className="ml-auto text-[10px] text-[var(--muted)]">{String(event.status || 'edited')}</span>
      </div>
    );
  }

  if (t === 'tool') {
    return (
      <div className="flex items-center gap-2 px-2 py-1">
        <FaWrench className="shrink-0 text-[var(--accent-primary)] text-[10px]" />
        <span className="text-[11px] truncate text-[var(--foreground)]">
          <span className="text-[var(--accent-primary)]">{String(event.name || 'tool')}</span>
          {event.title ? <span className="text-[var(--muted)]"> — {String(event.title)}</span> : null}
        </span>
        <span className={`ml-auto shrink-0 text-[10px] ${event.status === 'error' ? 'text-red-500' : 'text-[var(--muted)]'}`}>
          {String(event.status || '')}
        </span>
      </div>
    );
  }

  if (t === 'error') {
    return (
      <div className="flex items-start gap-2 px-2 py-1 text-red-500">
        <FaExclamationTriangle className="mt-0.5 shrink-0 text-[10px]" />
        <span className="text-[11px] whitespace-pre-wrap break-words">{String(event.message || '')}</span>
      </div>
    );
  }

  return null;
}

export default function CodeAgentPanel({ session }: CodeAgentPanelProps) {
  const { messages } = useLanguage();
  const { events, debugEvents, status, diffTick } = useCodeAgentEvents(session);
  const [tab, setTab] = useState<'activity' | 'diffs' | 'debug'>('activity');
  const [updating, setUpdating] = useState(false);
  const [updateResult, setUpdateResult] = useState<string | null>(null);
  const timelineRef = useRef<HTMLDivElement>(null);

  const updateAgent = async () => {
    if (updating) return;
    // Safe while the agent works: the new binary lands in the DATABASE
    // override and running agents keep the old version until their next
    // natural restart -- the backend no longer stops anything (the first
    // version did, and clicking Update mid-answer killed the session).
    setUpdating(true);
    setUpdateResult(null);
    try {
      const response = await fetch('/api/code/agent/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: 'latest' }),
      });
      const body = await response.json();
      if (response.ok) {
        let result = `${messages.codeAgent?.updated || 'Updated to'} v${body.version || '?'}`;
        if (body.pending_restart > 0) {
          result += ` — ${messages.codeAgent?.updatePending || 'active agents keep the previous version until they restart'}`;
        }
        setUpdateResult(result);
      } else {
        setUpdateResult(body?.detail?.message || `HTTP ${response.status}`);
      }
    } catch (error) {
      setUpdateResult(String(error));
    } finally {
      setUpdating(false);
    }
  };

  // Follow the newest activity (feeds render oldest -> newest).
  useEffect(() => {
    if ((tab === 'activity' || tab === 'debug') && timelineRef.current) {
      timelineRef.current.scrollTop = timelineRef.current.scrollHeight;
    }
  }, [events, debugEvents, tab]);

  const visibleEvents = events.filter((e) => e.t !== 'status' && e.t !== 'diff_hint');
  const crashEvent = events.findLast?.((e) => e.t === 'status' && e.state === 'crashed');

  if (!session) {
    return (
      <div className="h-full flex items-center justify-center p-6 text-center">
        <div className="max-w-sm space-y-2">
          <FaTerminal className="mx-auto text-2xl text-[var(--accent-primary)]/60" />
          <p className="text-sm text-[var(--muted)]">
            {messages.codeAgent?.waitingForSession ||
              'Send a message in the chat to start the code agent. Its edits, diffs and commands will appear here live.'}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col min-h-0">
      {/* Header: where the agent works + status + abort */}
      <div className="px-3 py-2 border-b border-[var(--border-color)] shrink-0 space-y-1">
        <div className="flex items-center gap-2">
          <FaCircle className={`text-[8px] ${STATUS_COLORS[status]}`} />
          <code
            className="text-[11px] font-mono truncate text-[var(--muted)]"
            title={session.repo_dir}
          >
            {session.repo_dir}
          </code>
          <button
            onClick={() => abortCodeSession(session.repo_key, session.session_id)}
            className="ml-auto shrink-0 flex items-center gap-1 px-2 py-0.5 rounded border border-red-500/40 text-red-500 hover:bg-red-500/10 transition-colors text-[10px]"
            title={messages.codeAgent?.abortTooltip || 'Stop the agent’s current operation'}
          >
            <FaStop className="text-[8px]" />
            {messages.codeAgent?.abort || 'Stop'}
          </button>
        </div>
        <div className="flex items-center gap-2 text-[10px] text-[var(--muted)] flex-wrap">
          {session.model_target && (
            <code className="font-mono" title={session.model_target}>{session.model_target}</code>
          )}
          {session.opencode_version && <span>opencode v{session.opencode_version}</span>}
          <button
            onClick={updateAgent}
            disabled={updating}
            className="underline decoration-dotted hover:text-[var(--accent-primary)] transition-colors disabled:opacity-50"
            title={messages.codeAgent?.updateTooltip || 'Download the latest opencode release into the app data folder (survives app updates)'}
          >
            {updating
              ? (messages.codeAgent?.updating || 'Updating…')
              : (messages.codeAgent?.update || 'Update agent')}
          </button>
          {updateResult && <span>{updateResult}</span>}
          {session.active_sessions > 1 && (
            <span className="text-yellow-500">
              {(messages.codeAgent?.multiSession || '{n} active sessions on this repo').replace('{n}', String(session.active_sessions))}
            </span>
          )}
          {session.is_local_type && (
            <span className="text-yellow-500 flex items-center gap-1">
              <FaExclamationTriangle className="text-[9px]" />
              {messages.codeAgent?.localEditWarning || 'Editing your local directory in place'}
            </span>
          )}
        </div>
        {session.version_warning && (
          <div className="text-[10px] text-yellow-500 flex items-start gap-1">
            <FaExclamationTriangle className="mt-0.5 shrink-0 text-[9px]" />
            <span>{session.version_warning}</span>
          </div>
        )}
        {crashEvent && (
          <div className="text-[10px] text-red-500">
            {messages.codeAgent?.crashed || 'The agent process exited. Send a message in the chat to restart it.'}
          </div>
        )}
      </div>

      {/* Tabs */}
      <div className="flex border-b border-[var(--border-color)] shrink-0 text-xs">
        {(['activity', 'diffs', 'debug'] as const).map((key) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`px-4 py-2 transition-colors ${
              tab === key
                ? 'text-[var(--accent-primary)] border-b-2 border-[var(--accent-primary)] -mb-px'
                : 'text-[var(--muted)] hover:text-[var(--foreground)]'
            }`}
          >
            {key === 'activity'
              ? (messages.codeAgent?.activityTab || 'Activity')
              : key === 'diffs'
                ? (messages.codeAgent?.diffsTab || 'Diffs')
                : (messages.codeAgent?.debugTab || 'Debug')}
          </button>
        ))}
      </div>

      {/* Body */}
      <div ref={timelineRef} className="flex-1 overflow-y-auto min-h-0">
        {tab === 'activity' ? (
          visibleEvents.length > 0 ? (
            <div className="p-2 space-y-1">
              {visibleEvents.map((event, index) => (
                <ActivityItem key={index} event={event} />
              ))}
            </div>
          ) : (
            <div className="p-6 text-center text-xs text-[var(--muted)]">
              {messages.codeAgent?.noActivity || 'No agent activity yet in this session.'}
            </div>
          )
        ) : tab === 'diffs' ? (
          <CodeDiffView
            repoKey={session.repo_key}
            sessionId={session.session_id}
            diffTick={diffTick}
          />
        ) : (
          /* Debug: the unfiltered firehose -- every bus event opencode emits
             (thinking/text deltas, every tool state change, session
             bookkeeping), timestamped, so "slow" is diagnosable at a glance. */
          debugEvents.length > 0 ? (
            <div className="p-2 font-mono text-[10px] leading-relaxed">
              {debugEvents.map((event, index) => (
                <div key={index} className="flex gap-2 py-0.5 border-b border-[var(--border-color)]/20">
                  <span className="shrink-0 text-[var(--muted)]/60">
                    {new Date(Number(event._ts) || Date.now()).toLocaleTimeString(undefined, { hour12: false })}
                  </span>
                  <span className="shrink-0 text-[var(--accent-primary)]">{String(event.type || '')}</span>
                  <span className="text-[var(--muted)] whitespace-pre-wrap break-all">{String(event.summary || '')}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="p-6 text-center text-xs text-[var(--muted)]">
              {messages.codeAgent?.noDebugEvents || 'No events yet. Everything the agent does and thinks will stream here, raw.'}
            </div>
          )
        )}
      </div>
    </div>
  );
}
