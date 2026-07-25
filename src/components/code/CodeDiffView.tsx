'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { FaChevronRight, FaSyncAlt } from 'react-icons/fa';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { tomorrow } from 'react-syntax-highlighter/dist/cjs/styles/prism';
import { useLanguage } from '@/contexts/LanguageContext';

// opencode's GET /session/:id/diff returns FileDiff[] -- field names have
// shifted across versions, so read them defensively.
interface FileDiff {
  file?: string;
  path?: string;
  filename?: string;
  additions?: number;
  added?: number;
  deletions?: number;
  removed?: number;
  patch?: string;
  diff?: string;
  before?: string;
  after?: string;
  [key: string]: unknown;
}

interface CodeDiffViewProps {
  repoKey: string;
  sessionId: string;
  // Bumped by the events hook whenever the working tree likely changed;
  // triggers a debounced refetch.
  diffTick: number;
  authorizationCode?: string;
}

const diffFile = (d: FileDiff) => d.file || d.path || d.filename || 'unknown file';
const diffPatch = (d: FileDiff) => d.patch || d.diff || '';

export default function CodeDiffView({
  repoKey,
  sessionId,
  diffTick,
  authorizationCode,
}: CodeDiffViewProps) {
  const { messages } = useLanguage();
  const [diffs, setDiffs] = useState<FileDiff[]>([]);
  const [loading, setLoading] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const debounceRef = useRef<number | undefined>(undefined);

  const fetchDiffs = useCallback(async () => {
    setLoading(true);
    try {
      const headers: Record<string, string> = {};
      if (authorizationCode) {
        headers['X-HackDeepWiki-Authorization'] = authorizationCode;
      }
      const response = await fetch(
        `/api/code/diff?repo_key=${encodeURIComponent(repoKey)}&session_id=${encodeURIComponent(sessionId)}`,
        { headers }
      );
      if (response.ok) {
        const data = await response.json();
        setDiffs(Array.isArray(data) ? data : []);
      }
    } catch (error) {
      console.warn('Diff fetch failed:', error);
    } finally {
      setLoading(false);
    }
  }, [repoKey, sessionId, authorizationCode]);

  useEffect(() => {
    // Debounce: a burst of file edits triggers one refetch, 1s after the
    // last hint.
    if (debounceRef.current) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(fetchDiffs, diffTick === 0 ? 0 : 1000);
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
    };
  }, [diffTick, fetchDiffs]);

  return (
    <div className="p-3 space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs text-[var(--muted)]">
          {diffs.length > 0
            ? `${diffs.length} ${messages.codeAgent?.filesChanged || 'file(s) changed'}`
            : (messages.codeAgent?.noChanges || 'No changes in this session yet.')}
        </span>
        <button
          onClick={fetchDiffs}
          className="text-[var(--muted)] hover:text-[var(--accent-primary)] transition-colors p-1"
          title={messages.codeAgent?.refresh || 'Refresh'}
          aria-label={messages.codeAgent?.refresh || 'Refresh'}
        >
          <FaSyncAlt className={`text-xs ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {diffs.map((diff, index) => {
        const file = diffFile(diff);
        const patch = diffPatch(diff);
        const isCollapsed = collapsed[file] ?? false;
        const additions = diff.additions ?? diff.added;
        const deletions = diff.deletions ?? diff.removed;
        return (
          <div key={`${file}-${index}`} className="rounded-md border border-[var(--border-color)]/60 overflow-hidden">
            <button
              onClick={() => setCollapsed((prev) => ({ ...prev, [file]: !isCollapsed }))}
              className="w-full flex items-center gap-2 px-2.5 py-1.5 text-xs bg-[var(--background)]/40 hover:bg-[var(--background)]/70 transition-colors"
            >
              <FaChevronRight className={`h-2.5 w-2.5 shrink-0 transition-transform ${isCollapsed ? '' : 'rotate-90'}`} />
              <span className="font-mono truncate text-[var(--foreground)]">{file}</span>
              <span className="ml-auto shrink-0 space-x-1.5">
                {typeof additions === 'number' && <span className="text-green-500">+{additions}</span>}
                {typeof deletions === 'number' && <span className="text-red-500">-{deletions}</span>}
              </span>
            </button>
            {!isCollapsed && (
              patch ? (
                <SyntaxHighlighter
                  language="diff"
                  style={tomorrow}
                  customStyle={{ margin: 0, fontSize: '0.72rem', maxHeight: '50vh' }}
                >
                  {patch}
                </SyntaxHighlighter>
              ) : (
                <div className="px-2.5 py-2 text-xs text-[var(--muted)] italic">
                  {messages.codeAgent?.noPatchBody || 'Content changed (no textual diff available).'}
                </div>
              )
            )}
          </div>
        );
      })}
    </div>
  );
}
