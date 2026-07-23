import { NextRequest, NextResponse } from 'next/server';
import { Agent } from 'undici';

// The target backend server base URL, derived from environment variable or defaulted.
// This should match the logic in your frontend's page.tsx for consistency.
const TARGET_SERVER_BASE_URL = process.env.SERVER_BASE_URL || 'http://localhost:8001';

// The backend blocks on api.simple_chat.chat_completions_stream's
// request_rag.prepare_retriever(...) call -- which runs the full RAG/
// embedding pipeline synchronously -- BEFORE it returns the
// StreamingResponse, so this proxy's fetch() receives zero response bytes
// (not even headers) until that finishes. For a large repo with a slow
// local embedder (Ollama, CPU-bound, one batch at a time), that can easily
// exceed undici's default 5-minute headersTimeout, throwing
// UND_ERR_HEADERS_TIMEOUT even though the backend is still working fine.
// This is the HTTP fallback path only (the primary path is the WebSocket
// handler in websocket_wiki.py, which has no such headers-timeout concept),
// but it should still succeed rather than fail on a slow embedder.
const longHeadersTimeoutAgent = new Agent({
  headersTimeout: 30 * 60 * 1000, // 30 minutes
  bodyTimeout: 0, // no limit once the stream starts
});

// This is a fallback HTTP implementation that will be used if WebSockets are not available
// or if there's an error with the WebSocket connection
export async function POST(req: NextRequest) {
  try {
    const requestBody = await req.json(); // Assuming the frontend sends JSON

    // Note: This endpoint now uses the HTTP fallback instead of WebSockets
    // The WebSocket implementation is in src/utils/websocketClient.ts
    // This HTTP endpoint is kept for backward compatibility
    console.log('Using HTTP fallback for chat completion instead of WebSockets');

    const targetUrl = `${TARGET_SERVER_BASE_URL}/chat/completions/stream`;

    // Make the actual request to the backend service
    const backendResponse = await fetch(targetUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream', // Indicate that we expect a stream
      },
      body: JSON.stringify(requestBody),
      // @ts-expect-error -- `dispatcher` is a Node/undici-specific fetch
      // extension not present in the standard lib.dom fetch types, but
      // Next.js's server-side fetch is undici under the hood and does
      // respect it.
      dispatcher: longHeadersTimeoutAgent,
    });

    // If the backend service returned an error, forward that error to the client
    if (!backendResponse.ok) {
      const errorBody = await backendResponse.text();
      const errorHeaders = new Headers();
      backendResponse.headers.forEach((value, key) => {
        errorHeaders.set(key, value);
      });
      return new NextResponse(errorBody, {
        status: backendResponse.status,
        statusText: backendResponse.statusText,
        headers: errorHeaders,
      });
    }

    // Ensure the backend response has a body to stream
    if (!backendResponse.body) {
      return new NextResponse('Stream body from backend is null', { status: 500 });
    }

    // Create a new ReadableStream to pipe the data from the backend to the client
    const stream = new ReadableStream({
      async start(controller) {
        const reader = backendResponse.body!.getReader();
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) {
              break;
            }
            controller.enqueue(value);
          }
        } catch (error) {
          console.error('Error reading from backend stream in proxy:', error);
          controller.error(error);
        } finally {
          controller.close();
          reader.releaseLock(); // Important to release the lock on the reader
        }
      },
      cancel(reason) {
        console.log('Client cancelled stream request:', reason);
      }
    });

    // Set up headers for the response to the client
    const responseHeaders = new Headers();
    // Copy the Content-Type from the backend response (e.g., 'text/event-stream')
    const contentType = backendResponse.headers.get('Content-Type');
    if (contentType) {
      responseHeaders.set('Content-Type', contentType);
    }
    // It's good practice for streams not to be cached or transformed by intermediaries.
    responseHeaders.set('Cache-Control', 'no-cache, no-transform');

    return new NextResponse(stream, {
      status: backendResponse.status, // Should be 200 for a successful stream start
      headers: responseHeaders,
    });

  } catch (error) {
    console.error('Error in API proxy route (/api/chat/stream):', error);
    let errorMessage = 'Internal Server Error in proxy';
    if (error instanceof Error) {
      errorMessage = error.message;
    }
    return new NextResponse(JSON.stringify({ error: errorMessage }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

// Optional: Handle OPTIONS requests for CORS if you ever call this from a different origin
// or use custom headers that trigger preflight requests. For same-origin, it's less critical.
export async function OPTIONS() {
  return new NextResponse(null, {
    status: 204, // No Content
    headers: {
      'Access-Control-Allow-Origin': '*', // Be more specific in production if needed
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization', // Adjust as per client's request headers
    },
  });
}