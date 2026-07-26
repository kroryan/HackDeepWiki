import { NextRequest, NextResponse } from "next/server";

const TARGET_SERVER_BASE_URL = process.env.SERVER_BASE_URL || 'http://localhost:8001';

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    
    // Forward the request to the backend API
    const response = await fetch(`${TARGET_SERVER_BASE_URL}/auth/validate`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });
    
    if (!response.ok) {
      return NextResponse.json(
        { error: `Backend server returned ${response.status}` },
        { status: response.status }
      );
    }
    
    const data = await response.json();
    const result = NextResponse.json(data);
    if (data?.success && typeof data?.session_token === 'string') {
      const configuredLifetime = Number(
        process.env.HACKDEEPWIKI_AUTH_SESSION_SECONDS || 8 * 60 * 60,
      );
      result.cookies.set('hackdeepwiki_session', data.session_token, {
        httpOnly: true,
        sameSite: 'strict',
        secure: request.nextUrl.protocol === 'https:',
        path: '/',
        maxAge: Number.isFinite(configuredLifetime)
          ? configuredLifetime
          : 8 * 60 * 60,
      });
    }
    return result;
  } catch (error) {
    console.error('Error forwarding request to backend:', error);
    return NextResponse.json(
      { error: 'Internal Server Error' },
      { status: 500 }
    );
  }
}
