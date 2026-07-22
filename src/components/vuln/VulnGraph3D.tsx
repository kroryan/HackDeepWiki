'use client';

import React, { Component, ReactNode, useEffect, useState } from 'react';
import dynamic from 'next/dynamic';
import { FaCompress, FaExpand } from 'react-icons/fa';
import { GraphData, GraphNode } from './types';
import VulnGraph2D from './VulnGraph2D';

interface Props {
  graph: GraphData;
  onNodeClick?: (node: GraphNode) => void;
  height?: number | string;
}

// Load the Three.js graph client-side only (never during SSR).
const VulnGraph3DInner = dynamic(
  () => import('./VulnGraph3DInner'),
  {
    ssr: false,
    loading: () => (
      <div className="flex items-center justify-center text-[var(--muted)] text-sm h-full">
        Loading 3D graph…
      </div>
    ),
  },
);

type Mode = '3d' | '2d';

export default function VulnGraph3D({ graph, onNodeClick, height = 460 }: Props) {
  const [mode, setMode] = useState<Mode>('3d');
  const [isMaximized, setIsMaximized] = useState(false);

  // Esc exits fullscreen -- matches the convention of every other fullscreen
  // viewer in the app (Mermaid's diagram modal, the code reader, etc.).
  useEffect(() => {
    if (!isMaximized) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setIsMaximized(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [isMaximized]);

  const effectiveHeight = isMaximized ? '100%' : height;

  const toolbar = (
    <div className="flex items-center gap-2 mb-2">
      <div className="inline-flex rounded-md border border-[var(--border-color)] overflow-hidden text-xs">
        <button
          type="button"
          onClick={() => setMode('3d')}
          className={`px-3 py-1 ${mode === '3d' ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--muted)] hover:bg-[var(--background)]'}`}
        >
          3D
        </button>
        <button
          type="button"
          onClick={() => setMode('2d')}
          className={`px-3 py-1 ${mode === '2d' ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--muted)] hover:bg-[var(--background)]'}`}
        >
          2D (Mermaid)
        </button>
      </div>
      <span className="text-[11px] text-[var(--muted)]">
        click a CVE node for details · drag to rotate · scroll to zoom
      </span>
      <button
        type="button"
        onClick={() => setIsMaximized((m) => !m)}
        title={isMaximized ? 'Exit fullscreen (Esc)' : 'Fullscreen'}
        aria-label={isMaximized ? 'Exit fullscreen' : 'Fullscreen'}
        className="ml-auto flex items-center gap-1.5 px-2.5 py-1 text-xs rounded-md border border-[var(--border-color)] text-[var(--muted)] hover:text-[var(--accent-primary)] hover:border-[var(--accent-primary)] transition-colors"
      >
        {isMaximized ? <FaCompress /> : <FaExpand />}
        {isMaximized ? 'Exit fullscreen' : 'Fullscreen'}
      </button>
    </div>
  );

  const graphArea = (
    <div className={isMaximized ? 'flex-1 min-h-0' : ''}>
      {mode === '3d' ? (
        <ErrorBoundaryFallback onFallBack={() => setMode('2d')} height={effectiveHeight}>
          <VulnGraph3DInner graph={graph} onNodeClick={onNodeClick} height={effectiveHeight} />
        </ErrorBoundaryFallback>
      ) : (
        <VulnGraph2D graph={graph} height={effectiveHeight} />
      )}
    </div>
  );

  if (isMaximized) {
    return (
      <div className="fixed inset-0 z-[100] bg-[var(--background)] p-4 flex flex-col">
        {toolbar}
        {graphArea}
      </div>
    );
  }

  return (
    <div>
      {toolbar}
      {graphArea}
    </div>
  );
}

// --- Error boundary: if Three.js blows up at runtime, drop to the 2D view ---

interface EBProps {
  children: ReactNode;
  onFallBack: () => void;
  height: number | string;
}
interface EBState {
  hasError: boolean;
}

class ErrorBoundaryFallback extends Component<EBProps, EBState> {
  state: EBState = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error: unknown) {
    console.warn('3D vuln graph failed, falling back to 2D:', error);
    this.props.onFallBack();
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex items-center justify-center text-[var(--muted)] text-sm"
             style={{ height: this.props.height }}>
          3D graph unavailable — switching to 2D…
        </div>
      );
    }
    return this.props.children;
  }
}