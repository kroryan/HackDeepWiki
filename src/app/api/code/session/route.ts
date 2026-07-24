import { NextRequest, NextResponse } from 'next/server';

const TARGET_SERVER_BASE_URL = process.env.SERVER_BASE_URL || 'http://localhost:8001';

// Code Editing mode: ensure the opencode server+session for a repo. Quick
// JSON call (spawn + health poll worst case ~30s), no streaming involved.
export async function POST(req: NextRequest) {
  try {
    const body = await req.json();
    const backendResponse = await fetch(`${TARGET_SERVER_BASE_URL}/api/code/session`, {
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
    console.error('Error in /api/code/session proxy:', error);
    return NextResponse.json(
      { detail: { code: 'proxy_error', message: String(error) } },
      { status: 500 }
    );
  }
}
