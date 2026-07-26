/**
 * Server-only runtime bootstrap.
 *
 * Every Next.js route handler that talks to the bundled FastAPI process uses
 * global fetch.  Decorating those loopback requests here keeps the internal
 * proxy credential in one place instead of duplicating it across ~50 route
 * handlers. External GitHub/provider requests are left untouched.
 */

const PATCH_MARKER = Symbol.for('hackdeepwiki.authenticated-backend-fetch');

export async function register() {
  if (process.env.NEXT_RUNTIME !== 'nodejs') return;

  const runtimeGlobal = globalThis as typeof globalThis & {
    [PATCH_MARKER]?: boolean;
  };
  if (runtimeGlobal[PATCH_MARKER]) return;

  const originalFetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const rawUrl =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url;
    const backend = (process.env.SERVER_BASE_URL || 'http://localhost:8001').replace(/\/+$/, '');
    let targetsBackend = false;
    try {
      targetsBackend = new URL(rawUrl).origin === new URL(backend).origin;
    } catch {
      // Relative URLs are browser/Next-local calls, never direct backend calls.
    }

    const proxyToken = process.env.HACKDEEPWIKI_INTERNAL_PROXY_TOKEN;
    if (!targetsBackend || !proxyToken) {
      return originalFetch(input, init);
    }

    const headers = new Headers(
      input instanceof Request ? input.headers : undefined,
    );
    if (init?.headers) {
      new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    }
    headers.set('X-HackDeepWiki-Internal-Proxy', proxyToken);
    return originalFetch(input, { ...init, headers });
  };
  runtimeGlobal[PATCH_MARKER] = true;
}
