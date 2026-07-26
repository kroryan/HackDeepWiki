import { describe, expect, it } from 'vitest';
import {
  initialWikiWorkspaceState,
  wikiWorkspaceReducer,
} from './wikiWorkspaceReducer';

describe('wikiWorkspaceReducer', () => {
  it('clears stale failure state when a new load starts', () => {
    const failed = wikiWorkspaceReducer(initialWikiWorkspaceState(), {
      type: 'failed',
      error: 'offline',
      connection: true,
    });
    const loading = wikiWorkspaceReducer(failed, {
      type: 'loading',
      message: 'Retrying',
    });
    expect(loading).toMatchObject({
      isLoading: true,
      error: null,
      connectionError: false,
      loadingMessage: 'Retrying',
    });
  });

  it('supports React functional setter semantics', () => {
    const state = initialWikiWorkspaceState();
    const next = wikiWorkspaceReducer(state, {
      type: 'set',
      key: 'isLoading',
      value: (current: boolean) => !current,
    });
    expect(next.isLoading).toBe(false);
  });
});
