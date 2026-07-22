import { NextRequest, NextResponse } from 'next/server';

// Proxy to the backend's GET /api/vuln_cache/releases, which lists every
// saved dependency vulnerability scan release for the Scan History dropdown.
// Read at request time so the portable app's dynamically-chosen backend
// port works -- mirrors src/app/api/wiki_cache/releases/route.ts.
const getBackendBaseUrl = () => process.env.SERVER_BASE_URL || 'http://localhost:8001';

export async function GET(req: NextRequest) {
  try {
    const backendUrl = `${getBackendBaseUrl()}/api/vuln_cache/releases${req.nextUrl.search}`;
    const response = await fetch(backendUrl);
    const data = await response.text();
    return new NextResponse(data, {
      status: response.status,
      headers: { 'Content-Type': response.headers.get('Content-Type') || 'application/json' },
    });
  } catch (error) {
    console.error('Error proxying GET /api/vuln_cache/releases:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
