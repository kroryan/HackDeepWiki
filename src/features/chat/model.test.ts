import { describe, expect, it } from 'vitest';
import {
  appendProcessEvents,
  buildResearchContinueMessage,
  isResearchContinueMessage,
} from './model';

describe('chat domain model', () => {
  it('merges streamed thinking deltas and tool state transitions', () => {
    const result = appendProcessEvents(
      [{ kind: 'thinking', payload: { text: 'one ' } }],
      [
        { kind: 'thinking', payload: { text: 'two' } },
        { kind: 'tool', payload: { label: 'read', status: 'running' } },
        { kind: 'tool', payload: { label: 'read', status: 'completed' } },
      ],
    );
    expect(result).toHaveLength(2);
    expect(result[0].payload.text).toBe('one two');
    expect(result[1].payload.status).toBe('completed');
  });

  it('keeps synthetic research prompts out of the transcript', () => {
    const prompt = buildResearchContinueMessage('How does indexing work?');
    expect(prompt).toContain('How does indexing work?');
    expect(isResearchContinueMessage(prompt)).toBe(true);
    expect(isResearchContinueMessage('normal user question')).toBe(false);
  });
});
