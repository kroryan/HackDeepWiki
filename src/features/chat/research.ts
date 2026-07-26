import type { ResearchStage } from './model';

export const MAX_RESEARCH_ITERATIONS = 5;
export const RESEARCH_CONTINUE_SENTINEL =
  '[DEEP RESEARCH] Continue the research';

export function researchIsComplete(content: string): boolean {
  if (content.includes('## Final Conclusion')) return true;

  if (
    (content.includes('## Conclusion') || content.includes('## Summary')) &&
    !content.includes('I will now proceed to') &&
    !content.includes('Next Steps') &&
    !content.includes('next iteration')
  ) {
    return true;
  }

  if (
    content.includes('This concludes our research') ||
    content.includes('This completes our investigation') ||
    content.includes('This concludes the deep research process') ||
    content.includes('Key Findings and Implementation Details') ||
    content.includes('In conclusion,') ||
    (content.includes('Final') && content.includes('Conclusion'))
  ) {
    return true;
  }

  return (
    content.includes('Dockerfile') &&
    (content.includes('This Dockerfile') || content.includes('The Dockerfile')) &&
    !content.includes('Next Steps') &&
    !content.includes('In the next iteration')
  );
}

export function extractResearchStage(
  content: string,
  iteration: number,
): ResearchStage | null {
  if (
    iteration === 1 &&
    /## Research Plan([\s\S]*?)(?:## Next Steps|$)/.test(content)
  ) {
    return {
      title: 'Research Plan',
      content,
      iteration: 1,
      type: 'plan',
    };
  }

  if (
    iteration >= 1 &&
    iteration <= 4 &&
    new RegExp(
      `## Research Update ${iteration}([\\s\\S]*?)(?:## Next Steps|$)`,
    ).test(content)
  ) {
    return {
      title: `Research Update ${iteration}`,
      content,
      iteration,
      type: 'update',
    };
  }

  if (/## Final Conclusion([\s\S]*?)$/.test(content)) {
    return {
      title: 'Final Conclusion',
      content,
      iteration,
      type: 'conclusion',
    };
  }
  return null;
}

export function buildResearchContinueMessage(originalTopic: string): string {
  const topic = (originalTopic || '')
    .trim()
    .replace(/^\[DEEP RESEARCH\]\s*/i, '');
  return topic
    ? `[DEEP RESEARCH] Continue the research on: ${topic}`
    : RESEARCH_CONTINUE_SENTINEL;
}

export function isResearchContinueMessage(content: string): boolean {
  if (!content) return false;
  if (content === RESEARCH_CONTINUE_SENTINEL) return true;
  return /^\[DEEP RESEARCH\]\s*Continue the research(\s+on:)?/i.test(content);
}
