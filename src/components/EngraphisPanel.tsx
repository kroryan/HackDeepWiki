'use client';

import React, { useEffect, useState } from 'react';
import { FaBrain, FaSync, FaExternalLinkAlt } from 'react-icons/fa';

interface EngraphisStatus {
  available: boolean;
  url?: string | null;
  dashboard_url?: string | null;
  workspace?: string;
  error?: string | null;
}

interface EngraphisPanelProps {
  owner: string;
  repo: string;
  /** Wiki release the user has open (memory is isolated per release). */
  wikiVersion?: number;
  /** 'version' = this release's memory; 'evolution' = cross-release changes. */
  view: 'version' | 'evolution';
}

/**
 * Engraphis memory dashboard embedded INSIDE HackDeepWiki. The dashboard is
 * Engraphis's own web UI, served by the backend process on a loopback port
 * (api/engraphis_integration.py) and deep-linked to the workspace that
 * matches what the user is looking at. No new window, no external address --
 * it renders in an iframe right in the content panel, and the caller
 * (page.tsx) provides the "back to wiki" button, same as Security Analysis.
 *
 * NOTE: iframe sizing uses inline styles on purpose -- Tailwind arbitrary
 * percentage classes get dropped from the built CSS in this project.
 */
export default function EngraphisPanel({ owner, repo, wikiVersion, view }: EngraphisPanelProps) {
  const [status, setStatus] = useState<EngraphisStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFetchError(null);
    setStatus(null);
    const params = new URLSearchParams({ owner, repo, view });
    if (wikiVersion != null) params.set('wiki_version', String(wikiVersion));
    fetch(`/api/engraphis/status?${params.toString()}`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`status ${res.status}`);
        return res.json();
      })
      .then((data: EngraphisStatus) => {
        if (!cancelled) setStatus(data);
      })
      .catch((err) => {
        if (!cancelled) setFetchError(String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [owner, repo, wikiVersion, view, reloadKey]);

  const url = status?.url || status?.dashboard_url || null;

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center p-10 text-[var(--muted)]">
        <FaBrain className="text-2xl mb-3 animate-pulse text-[var(--accent-primary)]" />
        <p className="text-sm font-mono">Starting Engraphis memory dashboard…</p>
      </div>
    );
  }

  if (fetchError || !status?.available || !url) {
    return (
      <div className="flex flex-col items-center justify-center p-10 text-center">
        <FaBrain className="text-2xl mb-3 text-[var(--muted)]" />
        <p className="text-sm text-[var(--foreground)] mb-1 font-serif">
          Engraphis memory is not available in this build.
        </p>
        <p className="text-xs text-[var(--muted)] font-mono mb-4">
          {fetchError || status?.error || 'The engraphis package is not installed.'}
        </p>
        <button
          onClick={() => setReloadKey((k) => k + 1)}
          className="flex items-center gap-1.5 text-xs font-mono px-2.5 py-1.5 rounded-md border border-[var(--border-color)] text-[var(--muted)] hover:text-[var(--accent-primary)] hover:border-[var(--accent-primary)] transition-colors"
        >
          <FaSync className="text-xs" /> Retry
        </button>
      </div>
    );
  }

  return (
    <div className="w-full">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[11px] font-mono text-[var(--muted)]">
          {view === 'evolution'
            ? `Cross-release evolution memory of ${owner}/${repo}`
            : `Memory of ${owner}/${repo} — wiki release v${wikiVersion ?? 0} (shared by chat & code editor)`}
          {status.workspace ? ` · workspace: ${status.workspace}` : ''}
        </span>
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          title="Open in a separate tab (optional)"
          className="flex items-center gap-1 text-[10px] font-mono text-[var(--muted)] hover:text-[var(--accent-primary)] transition-colors"
        >
          <FaExternalLinkAlt className="text-[9px]" />
        </a>
      </div>
      <iframe
        key={`${url}-${reloadKey}`}
        src={url}
        title="Engraphis memory dashboard"
        // Inline sizing: critical layout dimensions must not rely on Tailwind
        // arbitrary values in this project (they get purged from the build).
        style={{
          width: '100%',
          height: 'calc(100vh - 180px)',
          minHeight: '480px',
          border: '1px solid var(--border-color)',
          borderRadius: '8px',
          background: '#0b0e14',
        }}
      />
    </div>
  );
}
