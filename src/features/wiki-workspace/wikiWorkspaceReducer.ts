import type { Dispatch, SetStateAction } from 'react';

export interface WikiWorkspaceState {
  isLoading: boolean;
  loadingMessage?: string;
  error: string | null;
  embeddingError: boolean;
  connectionError: boolean;
  contentGenerationError: boolean;
  structureRequestInProgress: boolean;
}

export type WikiWorkspaceAction =
  | { type: 'loading'; message?: string }
  | { type: 'ready' }
  | { type: 'failed'; error: string; connection?: boolean; content?: boolean }
  | {
      type: 'set';
      key: keyof WikiWorkspaceState;
      value: unknown;
    };

export function initialWikiWorkspaceState(
  loadingMessage?: string,
): WikiWorkspaceState {
  return {
    isLoading: true,
    loadingMessage,
    error: null,
    embeddingError: false,
    connectionError: false,
    contentGenerationError: false,
    structureRequestInProgress: false,
  };
}

export function wikiWorkspaceReducer(
  state: WikiWorkspaceState,
  action: WikiWorkspaceAction,
): WikiWorkspaceState {
  switch (action.type) {
    case 'loading':
      return {
        ...state,
        isLoading: true,
        loadingMessage: action.message,
        error: null,
        connectionError: false,
        contentGenerationError: false,
      };
    case 'ready':
      return { ...state, isLoading: false, loadingMessage: undefined, error: null };
    case 'failed':
      return {
        ...state,
        isLoading: false,
        error: action.error,
        connectionError: Boolean(action.connection),
        contentGenerationError: Boolean(action.content),
      };
    case 'set': {
      const previous = state[action.key];
      const value: unknown =
        typeof action.value === 'function'
          ? (action.value as (current: unknown) => unknown)(previous)
          : action.value;
      return { ...state, [action.key]: value };
    }
  }
}

export type WorkspaceSetter<K extends keyof WikiWorkspaceState> = Dispatch<
  SetStateAction<WikiWorkspaceState[K]>
>;
