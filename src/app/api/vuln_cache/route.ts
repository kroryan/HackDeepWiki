import { NextRequest, NextResponse } from 'next/server';

// Proxy to the backend's dependency vulnerability scan cache. Read at
// request time (not `next build` time) so this works correctly with the
// portable app's dynamically-chosen backend port -- see
// src/app/api/wiki_cache/route.ts, which this mirrors.
const getBackendBaseUrl = () => process.env.SERVER_BASE_URL || 'http://localhost:8001';

export async function GET(req: NextRequest) {
  try {
    const backendUrl = `${getBackendBaseUrl()}/api/vuln_cache${req.nextUrl.search}`;
    const response = await fetch(backendUrl);
    const data = await response.text();
    return new NextResponse(data, {
      status: response.status,
      headers: { 'Content-Type': response.headers.get('Content-Type') || 'application/json' },
    });
  } catch (error) {
    console.error('Error proxying GET /api/vuln_cache:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}

export async function DELETE(req: NextRequest) {
  try {
    const backendUrl = `${getBackendBaseUrl()}/api/vuln_cache${req.nextUrl.search}`;
    const response = await fetch(backendUrl, { method: 'DELETE' });
    const data = await response.text();
    return new NextResponse(data, {
      status: response.status,
      headers: { 'Content-Type': response.headers.get('Content-Type') || 'application/json' },
    });
  } catch (error) {
    console.error('Error proxying DELETE /api/vuln_cache:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
