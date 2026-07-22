import { NextRequest, NextResponse } from 'next/server';

// Proxy to the backend's website security scan cache. Read at request time
// (not `next build` time) so this works correctly with the portable app's
// dynamically-chosen backend port -- mirrors src/app/api/vuln_cache/route.ts.
const getBackendBaseUrl = () => process.env.SERVER_BASE_URL || 'http://localhost:8001';

export async function GET(req: NextRequest) {
  try {
    const backendUrl = `${getBackendBaseUrl()}/api/web_vuln_cache${req.nextUrl.search}`;
    const response = await fetch(backendUrl);
    const data = await response.text();
    return new NextResponse(data, {
      status: response.status,
      headers: { 'Content-Type': response.headers.get('Content-Type') || 'application/json' },
    });
  } catch (error) {
    console.error('Error proxying GET /api/web_vuln_cache:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}

export async function DELETE(req: NextRequest) {
  try {
    const backendUrl = `${getBackendBaseUrl()}/api/web_vuln_cache${req.nextUrl.search}`;
    const response = await fetch(backendUrl, { method: 'DELETE' });
    const data = await response.text();
    return new NextResponse(data, {
      status: response.status,
      headers: { 'Content-Type': response.headers.get('Content-Type') || 'application/json' },
    });
  } catch (error) {
    console.error('Error proxying DELETE /api/web_vuln_cache:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
