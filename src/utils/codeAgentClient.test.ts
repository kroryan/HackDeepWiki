import { describe, expect, it } from 'vitest';

import { extractLatestAssistantText } from './codeAgentClient';


describe('CodeAgent durable message recovery', () => {
  it('extracts the latest assistant text from OpenCode info/parts messages', () => {
    const text = extractLatestAssistantText([
      { info: { role: 'user' }, parts: [{ type: 'text', text: 'do it' }] },
      {
        info: { role: 'assistant' },
        parts: [
          { type: 'reasoning', text: 'hidden' },
          { type: 'text', text: 'first answer' },
        ],
      },
      {
        info: { role: 'assistant' },
        parts: [
          { type: 'text', text: 'durable ' },
          { type: 'text', text: 'final answer' },
        ],
      },
    ]);

    expect(text).toBe('durable final answer');
  });

  it('supports the legacy top-level message shape and malformed payloads', () => {
    expect(extractLatestAssistantText([
      { role: 'assistant', content: 'legacy answer' },
    ])).toBe('legacy answer');
    expect(extractLatestAssistantText({ messages: [] })).toBe('');
  });
});
