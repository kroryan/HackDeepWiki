/**
 * Frontend counterpart to api/stream_events.py.
 *
 * The chat transports carry the model's answer as plain text over the
 * WebSocket/HTTP stream, but also interleave out-of-band "process" events
 * (tool calls, reasoning/"thinking" tokens) as self-terminating frames using
 * a control-character sentinel that never occurs in normal prose:
 *
 *   \x01FDW\x01<kind>\x02<json-payload>\x03
 *
 * StreamParser.feed() splits each incoming chunk into plain answer text
 * (frames removed) and any complete ProcessEvents found, buffering a
 * partial frame across chunk boundaries the same way the backend's own
 * sniff-and-relay buffers an ambiguous tool-call prefix -- a frame's start
 * sentinel, kind, or payload can each land in a different WebSocket message
 * or HTTP stream chunk.
 */

export interface ProcessEvent {
  kind: string;
  payload: Record<string, unknown>;
}

const PROC_INTRO = '\x01FDW\x01';
const PROC_FIELD = '\x02';
const PROC_END = '\x03';

export class StreamParser {
  private buffer = '';

  /** Feed one raw chunk from the stream. Returns the plain answer text
   * extracted so far (process frames removed) plus any complete process
   * events found in this chunk, in order. */
  feed(chunk: string): { text: string; events: ProcessEvent[] } {
    this.buffer += chunk;
    let text = '';
    const events: ProcessEvent[] = [];

    while (true) {
      const introIdx = this.buffer.indexOf(PROC_INTRO);
      if (introIdx === -1) {
        // No frame start in the buffer. The tail could still be the first
        // few characters of PROC_INTRO split across chunks -- hold those
        // back instead of flushing them as answer text.
        const holdBack = longestPartialSuffixOf(PROC_INTRO, this.buffer);
        text += this.buffer.slice(0, this.buffer.length - holdBack);
        this.buffer = this.buffer.slice(this.buffer.length - holdBack);
        break;
      }

      // Flush any plain text preceding the frame, then try to parse the
      // frame itself; if any part of it hasn't arrived yet, put back what
      // we have and wait for more data.
      text += this.buffer.slice(0, introIdx);
      const rest = this.buffer.slice(introIdx + PROC_INTRO.length);

      const fieldIdx = rest.indexOf(PROC_FIELD);
      if (fieldIdx === -1) {
        this.buffer = PROC_INTRO + rest;
        break;
      }
      const kind = rest.slice(0, fieldIdx);
      const afterField = rest.slice(fieldIdx + PROC_FIELD.length);

      const endIdx = afterField.indexOf(PROC_END);
      if (endIdx === -1) {
        this.buffer = PROC_INTRO + kind + PROC_FIELD + afterField;
        break;
      }

      const jsonPayload = afterField.slice(0, endIdx);
      try {
        events.push({ kind, payload: JSON.parse(jsonPayload) });
      } catch (e) {
        console.error('Failed to parse process event payload', e, jsonPayload);
      }
      this.buffer = afterField.slice(endIdx + PROC_END.length);
      // Loop again: there may be more text/frames already in the buffer.
    }

    return { text, events };
  }
}

/** Length of the longest suffix of `s` that is itself a (proper) prefix of
 * `needle` -- i.e. how much of `s`'s tail could still turn into `needle`
 * once more characters arrive. Used to avoid flushing a split sentinel as
 * answer text. */
function longestPartialSuffixOf(needle: string, s: string): number {
  const maxLen = Math.min(s.length, needle.length - 1);
  for (let len = maxLen; len > 0; len--) {
    if (needle.startsWith(s.slice(s.length - len))) return len;
  }
  return 0;
}
