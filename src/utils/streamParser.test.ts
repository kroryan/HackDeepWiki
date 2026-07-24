import { describe, it, expect } from 'vitest';
import { StreamParser } from './streamParser';

// The sentinel grammar (see streamParser.ts): a process frame is
//   \x01FDW\x01<kind>\x02<json-payload>\x03
// interleaved into the plain-text answer stream. These tests pin the parser's
// two contracts: (1) frames are removed from the answer text and emitted as
// structured events; (2) a frame split across chunks (and even a sentinel
// prefix split across chunks) is buffered, not flushed as answer text.

const INTRO = '\x01FDW\x01';
const FIELD = '\x02';
const END = '\x03';

function frame(kind: string, payload: unknown): string {
  return `${INTRO}${kind}${FIELD}${JSON.stringify(payload)}${END}`;
}

describe('StreamParser', () => {
  it('passes plain text through unchanged when no frames are present', () => {
    const p = new StreamParser();
    const out = p.feed('Hello, world.\nSecond line.');
    expect(out.text).toBe('Hello, world.\nSecond line.');
    expect(out.events).toEqual([]);
  });

  it('extracts a complete frame from surrounding text', () => {
    const p = new StreamParser();
    const out = p.feed(`before ${frame('tool_call', { name: 'SEARCH_WIKI', query: 'foo' })} after`);
    expect(out.text).toBe('before  after');
    expect(out.events).toHaveLength(1);
    expect(out.events[0]).toEqual({
      kind: 'tool_call',
      payload: { name: 'SEARCH_WIKI', query: 'foo' },
    });
  });

  it('emits multiple frames in order, preserving the text between them', () => {
    const p = new StreamParser();
    const chunk = `a${frame('thinking', { t: 'hmm' })}b${frame('tool_call', { name: 'X' })}c`;
    const out = p.feed(chunk);
    expect(out.text).toBe('abc');
    expect(out.events.map((e) => e.kind)).toEqual(['thinking', 'tool_call']);
  });

  it('buffers a frame split across chunks and only emits it once complete', () => {
    const p = new StreamParser();
    const full = `text ${frame('tool_call', { name: 'SEARCH_WIKI', query: 'q' })} tail`;
    const mid = Math.floor(full.length / 2);
    const out1 = p.feed(full.slice(0, mid));
    // Whatever arrived so far that isn't a complete frame is plain text; the
    // partial frame must NOT appear in the text.
    expect(out1.events).toEqual([]);
    expect(out1.text).not.toContain('SEARCH_WIKI');
    const out2 = p.feed(full.slice(mid));
    expect(out2.events).toHaveLength(1);
    expect(out2.events[0].payload).toEqual({ name: 'SEARCH_WIKI', query: 'q' });
    // Reassembling the two text chunks must equal the frame-stripped stream.
    expect(out1.text + out2.text).toBe('text  tail');
  });

  it('holds back a partial sentinel prefix instead of flushing it as text', () => {
    // Feed the intro one character at a time. None of those characters are
    // answer text -- they're the start of a frame that hasn't completed.
    const p = new StreamParser();
    let text = '';
    for (const ch of INTRO) {
      text += p.feed(ch).text;
    }
    expect(text).toBe('');
    // Complete the frame: the held-back intro is now part of the frame.
    const out = p.feed(`tool_call${FIELD}${JSON.stringify({ ok: true })}${END}done`);
    expect(out.events[0].payload).toEqual({ ok: true });
    expect(out.text).toBe('done');
  });

  it('does not misread ordinary text that merely contains the intro characters', () => {
    // A literal '\x01' that is NOT followed by the rest of the intro must be
    // treated as answer text once it's clear it isn't a frame start.
    const p = new StreamParser();
    const out = p.feed('plain\x01not-a-frame\x02also-plain\x03end');
    // The lone \x01 isn't the full intro, so no frame is parsed; everything is text.
    expect(out.events).toEqual([]);
    expect(out.text).toBe('plain\x01not-a-frame\x02also-plain\x03end');
  });

  it('skips a malformed frame payload (bad JSON) without throwing', () => {
    const p = new StreamParser();
    const bad = `${INTRO}bad_kind${FIELD}not-valid-json${END}after`;
    const out = p.feed(bad);
    expect(out.text).toBe('after');
    expect(out.events).toEqual([]); // bad payload dropped, not emitted
  });

  it('handles an empty payload object', () => {
    const p = new StreamParser();
    const out = p.feed(`x${frame('ping', {})}y`);
    expect(out.text).toBe('xy');
    expect(out.events[0].payload).toEqual({});
  });
});