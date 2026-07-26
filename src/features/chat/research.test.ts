import { describe, expect, it } from 'vitest';

import {
  buildResearchContinueMessage,
  extractResearchStage,
  isResearchContinueMessage,
  researchIsComplete,
} from './research';

describe('deep research state parsing', () => {
  it('recognizes terminal and non-terminal responses', () => {
    expect(researchIsComplete('## Final Conclusion\nDone')).toBe(true);
    expect(researchIsComplete('## Summary\nUseful findings')).toBe(true);
    expect(researchIsComplete('## Summary\nNext Steps')).toBe(false);
    expect(researchIsComplete('Dockerfile details\nIn the next iteration')).toBe(false);
  });

  it('extracts each supported stage', () => {
    expect(extractResearchStage('## Research Plan\nA\n## Next Steps', 1)?.type)
      .toBe('plan');
    expect(extractResearchStage('## Research Update 2\nB', 2)?.title)
      .toBe('Research Update 2');
    expect(extractResearchStage('## Final Conclusion\nC', 5)?.type)
      .toBe('conclusion');
    expect(extractResearchStage('still researching', 2)).toBeNull();
  });

  it('normalizes internal continuation messages', () => {
    const message = buildResearchContinueMessage('[DEEP RESEARCH] Topic');
    expect(message).toBe('[DEEP RESEARCH] Continue the research on: Topic');
    expect(isResearchContinueMessage(message)).toBe(true);
    expect(isResearchContinueMessage('normal question')).toBe(false);
  });
});
