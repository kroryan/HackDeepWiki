import { NextRequest, NextResponse } from 'next/server';

const TARGET_SERVER_BASE_URL = process.env.SERVER_BASE_URL || 'http://localhost:8001';

// Downloads an opencode release into DATABASE/opencode/bin (synchronous on
// the backend, ~30 MB archive) -- give it a generous window.
export const maxDuration = 300;

export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const backendResponse = await fetch(`${TARGET_SERVER_BASE_URL}/api/code/agent/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const text = await backendResponse.text();
    return new NextResponse(text, {
      status: backendResponse.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    console.error('Error in /api/code/agent/update proxy:', error);
    return NextResponse.json(
      { detail: { code: 'proxy_error', message: String(error) } },
      { status: 500 }
    );
  }
}
