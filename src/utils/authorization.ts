const STORAGE_KEY = 'hackdeepwiki_session';

export function getStoredAuthorization(): string {
  if (typeof window === 'undefined') return '';
  try {
    return window.sessionStorage.getItem(STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

export function storeAuthorization(value: string | null | undefined): void {
  if (typeof window === 'undefined') return;
  try {
    if (value) {
      window.sessionStorage.setItem(STORAGE_KEY, value);
    } else {
      window.sessionStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // Private browsing/storage denial must not crash the UI. The caller still
    // holds the token in React state for this page lifetime.
  }
}

export function withWebSocketAuthorization(
  rawUrl: string,
  explicit?: string,
): string {
  const token = explicit || getStoredAuthorization();
  if (!token) return rawUrl;
  const url = new URL(rawUrl);
  url.searchParams.set('authorization_code', token);
  return url.toString();
}
