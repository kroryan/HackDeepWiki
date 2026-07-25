import { NextRequest, NextResponse } from 'next/server';

const TARGET_SERVER_BASE_URL = process.env.SERVER_BASE_URL || 'http://localhost:8001';

// Engraphis embedded memory dashboard: availability + deep-linked URL for the
// requested workspace (per-wiki-release or the cross-release "evolution"
// one). The first backend call may lazily start the embedded server, so it
// can take a moment. force-dynamic: must re-read on every call.
export const dynamic = 'force-dynamic';

export async function GET(request: NextRequest) {
  try {
    const search = request.nextUrl.search || '';
    const backendResponse = await fetch(
      `${TARGET_SERVER_BASE_URL}/api/engraphis/status${search}`,
      { cache: 'no-store' }
    );
    const text = await backendResponse.text();
    return new NextResponse(text, {
      status: backendResponse.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (error) {
    console.error('Error in /api/engraphis/status proxy:', error);
    return NextResponse.json(
      { detail: { code: 'proxy_error', message: String(error) } },
      { status: 500 }
    );
  }
}
