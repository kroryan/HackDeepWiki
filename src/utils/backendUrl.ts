import { withWebSocketAuthorization } from '@/utils/authorization';

let backendBaseUrlPromise: Promise<string> | null = null;

export function getBackendBaseUrl(): Promise<string> {
  if (!backendBaseUrlPromise) {
    backendBaseUrlPromise = fetch('/api/runtime/backend', { cache: 'no-store' })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Runtime backend lookup failed: ${response.status}`);
        const data = await response.json();
        if (typeof data?.baseUrl !== 'string' || !/^https?:\/\//.test(data.baseUrl)) {
          throw new Error('Runtime backend lookup returned an invalid URL');
        }
        return data.baseUrl.replace(/\/+$/, '');
      })
      .catch((error) => {
        backendBaseUrlPromise = null;
        console.warn('Falling back to the default backend URL:', error);
        return 'http://localhost:8001';
      });
  }
  return backendBaseUrlPromise;
}

export async function getBackendWebSocketUrl(
  path: string,
  authorizationCode?: string,
): Promise<string> {
  const baseUrl = await getBackendBaseUrl();
  const wsBaseUrl = baseUrl.replace(/^https:/, 'wss:').replace(/^http:/, 'ws:');
  return withWebSocketAuthorization(
    `${wsBaseUrl}${path.startsWith('/') ? path : `/${path}`}`,
    authorizationCode,
  );
}
