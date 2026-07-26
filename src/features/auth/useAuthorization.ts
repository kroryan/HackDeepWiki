'use client';

import { apiJson } from '@/lib/apiClient';
import {
  getStoredAuthorization,
  storeAuthorization,
} from '@/utils/authorization';
import { useCallback, useEffect, useState } from 'react';

interface AuthStatus {
  auth_required: boolean;
}

export function useAuthorization() {
  const [authRequired, setAuthRequired] = useState(false);
  const [authorizationCode, setAuthorizationState] = useState(
    () => getStoredAuthorization(),
  );
  const [isAuthLoading, setIsAuthLoading] = useState(true);

  const setAuthorizationCode = useCallback((value: string) => {
    setAuthorizationState(value);
    storeAuthorization(value);
  }, []);

  useEffect(() => {
    let active = true;
    apiJson<AuthStatus>('/api/auth/status', { cache: 'no-store', timeoutMs: 10_000 })
      .then((status: AuthStatus) => {
        if (active) setAuthRequired(Boolean(status.auth_required));
      })
      .catch((error: unknown) => {
        console.error('Failed to fetch auth status:', error);
        if (active) setAuthRequired(true);
      })
      .finally(() => {
        if (active) setIsAuthLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  return {
    authRequired,
    authorizationCode,
    setAuthorizationCode,
    isAuthLoading,
  };
}
