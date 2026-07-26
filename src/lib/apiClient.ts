import { getStoredAuthorization } from '@/utils/authorization';

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export interface ApiRequestOptions extends RequestInit {
  timeoutMs?: number;
}

export async function apiFetch(
  input: string,
  options: ApiRequestOptions = {},
): Promise<Response> {
  const { timeoutMs = 60_000, signal, ...request } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, { once: true });
  const headers = new Headers(request.headers);
  const authorization = getStoredAuthorization();
  if (authorization && input.startsWith('/')) {
    headers.set('X-HackDeepWiki-Authorization', authorization);
  }
  try {
    return await fetch(input, {
      ...request,
      headers,
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}

export async function apiJson<T>(
  input: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const response = await apiFetch(input, options);
  const contentType = response.headers.get('content-type') || '';
  const data: unknown = contentType.includes('application/json')
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const detail =
      typeof data === 'object' && data && 'detail' in data
        ? (data as { detail?: unknown }).detail
        : data;
    throw new ApiError(
      typeof detail === 'string' ? detail : `Request failed (${response.status})`,
      response.status,
      detail,
    );
  }
  return data as T;
}
