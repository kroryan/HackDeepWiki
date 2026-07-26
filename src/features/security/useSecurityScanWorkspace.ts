'use client';

import { VulnScanStatus } from '@/components/vuln/types';
import { ScanRelease } from '@/utils/repoWikiHelpers';
import {
  Dispatch,
  SetStateAction,
  useCallback,
  useMemo,
  useReducer,
} from 'react';

export interface SecurityScanWorkspace<TReport> {
  report: TReport | null;
  status: VulnScanStatus;
  progressMessage: string | undefined;
  progressPercent: number | null;
  error: string | null;
  releases: ScanRelease[];
  selectedVersion: number | null;
  rescanModalOpen: boolean;
}

type FieldAction<TReport> = {
  field: keyof SecurityScanWorkspace<TReport>;
  value: unknown;
};

export function securityScanReducer<TReport>(
  state: SecurityScanWorkspace<TReport>,
  action: FieldAction<TReport>,
): SecurityScanWorkspace<TReport> {
  const previous = state[action.field];
  const value = typeof action.value === 'function'
    ? (action.value as (current: typeof previous) => typeof previous)(previous)
    : action.value;
  return { ...state, [action.field]: value };
}

const INITIAL_STATE: SecurityScanWorkspace<never> = {
  report: null,
  status: 'idle',
  progressMessage: undefined,
  progressPercent: null,
  error: null,
  releases: [],
  selectedVersion: null,
  rescanModalOpen: false,
};

export function useSecurityScanWorkspace<TReport>() {
  const [state, dispatch] = useReducer(
    securityScanReducer<TReport>,
    INITIAL_STATE as SecurityScanWorkspace<TReport>,
  );

  const setter = useCallback(
    <K extends keyof SecurityScanWorkspace<TReport>>(field: K) => (
      value: SetStateAction<SecurityScanWorkspace<TReport>[K]>,
    ) => dispatch({ field, value }),
    [],
  );

  const setters = useMemo(() => ({
    setReport: setter('report') as Dispatch<SetStateAction<TReport | null>>,
    setStatus: setter('status') as Dispatch<SetStateAction<VulnScanStatus>>,
    setProgressMessage: setter('progressMessage') as Dispatch<
      SetStateAction<string | undefined>
    >,
    setProgressPercent: setter('progressPercent') as Dispatch<
      SetStateAction<number | null>
    >,
    setError: setter('error') as Dispatch<SetStateAction<string | null>>,
    setReleases: setter('releases') as Dispatch<SetStateAction<ScanRelease[]>>,
    setSelectedVersion: setter('selectedVersion') as Dispatch<
      SetStateAction<number | null>
    >,
    setRescanModalOpen: setter('rescanModalOpen') as Dispatch<
      SetStateAction<boolean>
    >,
  }), [setter]);

  return useMemo(() => ({
    ...state,
    ...setters,
  }), [setters, state]);
}
