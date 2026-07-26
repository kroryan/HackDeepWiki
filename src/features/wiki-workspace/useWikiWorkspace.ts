'use client';

import { useCallback, useMemo, useReducer } from 'react';
import {
  initialWikiWorkspaceState,
  WikiWorkspaceState,
  wikiWorkspaceReducer,
  WorkspaceSetter,
} from './wikiWorkspaceReducer';

export function useWikiWorkspace(initialLoadingMessage?: string) {
  const [state, dispatch] = useReducer(
    wikiWorkspaceReducer,
    initialLoadingMessage,
    initialWikiWorkspaceState,
  );

  const setter = useCallback(
    <K extends keyof WikiWorkspaceState>(key: K): WorkspaceSetter<K> =>
      value => dispatch({ type: 'set', key, value: value as unknown }),
    [],
  );

  return useMemo(
    () => ({
      ...state,
      setIsLoading: setter('isLoading'),
      setLoadingMessage: setter('loadingMessage'),
      setError: setter('error'),
      setEmbeddingError: setter('embeddingError'),
      setConnectionError: setter('connectionError'),
      setContentGenerationError: setter('contentGenerationError'),
      setStructureRequestInProgress: setter('structureRequestInProgress'),
      startLoading: (message?: string) => dispatch({ type: 'loading', message }),
      markReady: () => dispatch({ type: 'ready' }),
      markFailed: (
        error: string,
        options: { connection?: boolean; content?: boolean } = {},
      ) => dispatch({ type: 'failed', error, ...options }),
    }),
    [setter, state],
  );
}
