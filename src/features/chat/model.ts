import type { ProcessEvent } from '@/utils/streamParser';

export interface Model {
  id: string;
  name: string;
}

export interface Provider {
  id: string;
  name: string;
  models: Model[];
  supportsCustomModel?: boolean;
}

export interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

export interface ResearchStage {
  title: string;
  content: string;
  iteration: number;
  type: 'plan' | 'update' | 'conclusion';
}

export interface ChatSession {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  messages: Message[];
  response: string;
  deepResearch: boolean;
  researchStages: ResearchStage[];
  currentStageIndex: number;
  researchIteration: number;
  researchComplete: boolean;
  codeMode?: boolean;
  includeSecurityContext?: boolean;
  codeSessionId?: string;
}

export const CODE_MODE_REPO_TYPES: readonly string[] = [
  'github',
  'gitlab',
  'bitbucket',
  'local',
] as const;

export function appendProcessEvents(
  previous: ProcessEvent[],
  incoming: ProcessEvent[],
): ProcessEvent[] {
  if (incoming.length === 0) return previous;
  const next = [...previous];
  for (const event of incoming) {
    const last = next[next.length - 1];
    if (last && event.kind === 'thinking' && last.kind === 'thinking') {
      next[next.length - 1] = {
        kind: 'thinking',
        payload: {
          ...last.payload,
          text:
            String(last.payload.text ?? '') + String(event.payload.text ?? ''),
        },
      };
      continue;
    }
    if (
      last &&
      event.kind === 'tool' &&
      last.kind === 'tool' &&
      String(last.payload.label ?? '') === String(event.payload.label ?? '')
    ) {
      next[next.length - 1] = event;
      continue;
    }
    next.push(event);
  }
  return next;
}

// Compatibility exports for older feature consumers; implementation lives in
// the focused research state module.
export {
  buildResearchContinueMessage,
  isResearchContinueMessage,
  MAX_RESEARCH_ITERATIONS,
} from './research';
