import { NextRequest } from 'next/server';

export const dynamic = 'force-dynamic';

type RouteContext = {
  params: Promise<{ path: string[] }>;
};

export async function GET(request: NextRequest, context: RouteContext) {
  const { path } = await context.params;
  const upstreamUrl = new URL(
    `/${path.map(encodeURIComponent).join('/')}`,
    'https://api.github.com'
  );
  request.nextUrl.searchParams.forEach((value, key) => {
    upstreamUrl.searchParams.append(key, value);
  });

  const requestAuthorization = request.headers.get('authorization');
  const configuredToken = process.env.GITHUB_TOKEN?.trim();
  const authorization =
    requestAuthorization ||
    (configuredToken ? `Bearer ${configuredToken}` : undefined);

  const headers: HeadersInit = {
    Accept: 'application/vnd.github+json',
    'User-Agent': 'HackDeepWiki',
    'X-GitHub-Api-Version': '2022-11-28',
  };
  if (authorization) {
    headers.Authorization = authorization;
  }

  const upstream = await fetch(upstreamUrl, {
    headers,
    cache: 'no-store',
  });
  const body = await upstream.text();

  if (
    upstream.status === 403 &&
    !authorization &&
    upstream.headers.get('x-ratelimit-remaining') === '0'
  ) {
    return Response.json(
      {
        message:
          'GitHub anonymous API rate limit exceeded. Configure GITHUB_TOKEN ' +
          'in hackdeepwiki.env or provide a token in the HackDeepWiki configuration.',
      },
      { status: 403 }
    );
  }

  const responseHeaders = new Headers();
  const contentType = upstream.headers.get('content-type');
  if (contentType) {
    responseHeaders.set('content-type', contentType);
  }
  for (const name of [
    'x-ratelimit-limit',
    'x-ratelimit-remaining',
    'x-ratelimit-reset',
    'etag',
  ]) {
    const value = upstream.headers.get(name);
    if (value) {
      responseHeaders.set(name, value);
    }
  }

  return new Response(body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}
