import { NextRequest, NextResponse } from 'next/server';

const AUTH_COOKIE = 'hackdeepwiki_session';
const SESSION_PREFIX = 'hdw1';

const PUBLIC_API_PATHS = new Set([
  '/api/auth/status',
  '/api/auth/validate',
  '/api/lang/config',
  '/api/models/config',
  '/api/runtime/backend',
]);

const authEnabled = () =>
  ['1', 'true', 't', 'yes', 'on'].includes(
    (process.env.HACKDEEPWIKI_AUTH_MODE || '').trim().toLowerCase(),
  );

const toBase64Url = (bytes: Uint8Array) => {
  let binary = '';
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
};

async function sessionIsValid(token: string | undefined): Promise<boolean> {
  const authCode = process.env.HACKDEEPWIKI_AUTH_CODE || '';
  if (!token || !authCode) return false;
  const parts = token.split('.');
  if (parts.length !== 4 || parts[0] !== SESSION_PREFIX) return false;
  const expires = Number(parts[1]);
  if (!Number.isSafeInteger(expires) || expires <= Math.floor(Date.now() / 1000)) {
    return false;
  }
  const payload = parts.slice(0, 3).join('.');
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(authCode),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const signature = new Uint8Array(
    await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(payload)),
  );
  const expected = toBase64Url(signature);
  if (expected.length !== parts[3].length) return false;
  let different = 0;
  for (let index = 0; index < expected.length; index += 1) {
    different |= expected.charCodeAt(index) ^ parts[3].charCodeAt(index);
  }
  return different === 0;
}

export async function proxy(request: NextRequest) {
  if (!authEnabled() || PUBLIC_API_PATHS.has(request.nextUrl.pathname)) {
    return NextResponse.next();
  }
  const token = request.cookies.get(AUTH_COOKIE)?.value;
  if (await sessionIsValid(token)) {
    return NextResponse.next();
  }
  return NextResponse.json(
    { detail: 'Authentication is required' },
    { status: 401 },
  );
}

export const config = {
  matcher: ['/api/:path*', '/local_repo/:path*', '/export/:path*'],
};
