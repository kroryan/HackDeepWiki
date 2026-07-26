import { describe, expect, it } from 'vitest';

import {
  SecurityScanWorkspace,
  securityScanReducer,
} from './useSecurityScanWorkspace';

describe('securityScanReducer', () => {
  it('supports direct and functional state transitions', () => {
    const initial: SecurityScanWorkspace<{ findings: number }> = {
      report: null,
      status: 'idle',
      progressMessage: undefined,
      progressPercent: null,
      error: null,
      releases: [],
      selectedVersion: null,
      rescanModalOpen: false,
    };
    const running = securityScanReducer(initial, {
      field: 'status',
      value: 'running',
    });
    const progressed = securityScanReducer(running, {
      field: 'progressPercent',
      value: (current: number | null) => (current ?? 0) + 25,
    });
    expect(running.status).toBe('running');
    expect(progressed.progressPercent).toBe(25);
    expect(initial.status).toBe('idle');
  });
});
