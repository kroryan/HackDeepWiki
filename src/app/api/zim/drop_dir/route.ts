import { NextResponse } from 'next/server';

const getBackendBaseUrl = () => process.env.SERVER_BASE_URL || 'http://localhost:8001';

export async function GET() {
  try {
    const response = await fetch(`${getBackendBaseUrl()}/api/zim/drop_dir`, { cache: 'no-store' });
    const data = await response.text();
    return new NextResponse(data, {
      status: response.status,
      headers: { 'Content-Type': response.headers.get('Content-Type') || 'application/json' },
    });
  } catch (error) {
    console.error('Error proxying GET /api/zim/drop_dir:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
