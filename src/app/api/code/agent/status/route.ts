import { NextRequest, NextResponse } from 'next/server';

const TARGET_SERVER_BASE_URL = process.env.SERVER_BASE_URL || 'http://localhost:8001';

// Runtime state of the embedded opencode binary (resolved path, versions,
// running instances). force-dynamic: must re-read on every call.
export const dynamic = 'force-dynamic';

export async function GET(req: NextRequest) {
  try {
    const headers: Record<string, string> = {};
    const authorization = req.headers.get('x-hackdeepwiki-authorization');
    if (authorization) headers['X-HackDeepWiki-Authorization'] = authorization;
    const backendResponse = await fetch(`${TARGET_SERVER_BASE_URL}/api/code/agent/status`, {
      cache: 'no-store',
      headers,
    });
    const text = await backendResponse.text();
    return new NextResponse(text, {
      status: backendResponse.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    console.error('Error in /api/code/agent/status proxy:', error);
    return NextResponse.json(
      { detail: { code: 'proxy_error', message: String(error) } },
      { status: 500 }
    );
  }
}
