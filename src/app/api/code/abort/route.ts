import { NextRequest, NextResponse } from 'next/server';

const TARGET_SERVER_BASE_URL = process.env.SERVER_BASE_URL || 'http://localhost:8001';

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const authorization = req.headers.get('x-hackdeepwiki-authorization');
    if (authorization) headers['X-HackDeepWiki-Authorization'] = authorization;
    const backendResponse = await fetch(`${TARGET_SERVER_BASE_URL}/api/code/abort`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
    const text = await backendResponse.text();
    return new NextResponse(text, {
      status: backendResponse.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    console.error('Error in /api/code/abort proxy:', error);
    return NextResponse.json(
      { detail: { code: 'proxy_error', message: String(error) } },
      { status: 500 }
    );
  }
}
