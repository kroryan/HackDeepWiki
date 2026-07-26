'use client';

import type { ChatSession, Message, ResearchStage } from './model';
import {
  Dispatch,
  SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

interface SessionSnapshot {
  messages: Message[];
  response: string;
  deepResearch: boolean;
  researchStages: ResearchStage[];
  currentStageIndex: number;
  researchIteration: number;
  researchComplete: boolean;
  codeMode: boolean;
  includeSecurityContext: boolean;
  codeSessionId?: string;
}

interface SessionSetters {
  question: Dispatch<SetStateAction<string>>;
  messages: Dispatch<SetStateAction<Message[]>>;
  response: Dispatch<SetStateAction<string>>;
  deepResearch: Dispatch<SetStateAction<boolean>>;
  researchStages: Dispatch<SetStateAction<ResearchStage[]>>;
  currentStageIndex: Dispatch<SetStateAction<number>>;
  researchIteration: Dispatch<SetStateAction<number>>;
  researchComplete: Dispatch<SetStateAction<boolean>>;
  includeSecurityContext: Dispatch<SetStateAction<boolean>>;
  codeSessionId: Dispatch<SetStateAction<string | undefined>>;
}

interface UseChatSessionsOptions {
  repo: { type: string; owner: string; repo: string };
  defaultTitle: string;
  snapshot: SessionSnapshot;
  setters: SessionSetters;
  onRestoreCodeMode?: (enabled: boolean) => void;
  codeModeAvailable: boolean;
}

const MAX_STORED_SESSIONS = 20;

export function useChatSessions({
  repo,
  defaultTitle,
  snapshot,
  setters,
  onRestoreCodeMode,
  codeModeAvailable,
}: UseChatSessionsOptions) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState('');
  const loadedSessionIdRef = useRef<string | null>(null);
  const storageKey = useMemo(
    () => `hackdeepwiki-chat-sessions:${repo.type}:${repo.owner}:${repo.repo}`,
    [repo.type, repo.owner, repo.repo],
  );
  const createSession = useCallback((title?: string): ChatSession => {
    const now = Date.now();
    return {
      id: `chat-${now}-${Math.random().toString(36).slice(2, 8)}`,
      title: title || defaultTitle,
      createdAt: now,
      updatedAt: now,
      messages: [],
      response: '',
      deepResearch: false,
      researchStages: [],
      currentStageIndex: 0,
      researchIteration: 0,
      researchComplete: false,
    };
  }, [defaultTitle]);

  useEffect(() => {
    loadedSessionIdRef.current = null;
    try {
      const stored = localStorage.getItem(storageKey);
      const parsed = stored ? JSON.parse(stored) as ChatSession[] : [];
      const valid = Array.isArray(parsed)
        ? parsed.filter(session => session?.id && Array.isArray(session.messages))
        : [];
      let initial = valid.length > 0 ? valid : [createSession()];
      if (initial[0].messages.length > 0) {
        initial = [createSession(), ...initial];
      }
      setSessions(initial);
      setActiveSessionId(initial[0].id);
    } catch (error) {
      console.error('Failed to load chat sessions:', error);
      const initial = createSession();
      setSessions([initial]);
      setActiveSessionId(initial.id);
    }
  }, [createSession, storageKey]);

  useEffect(() => {
    if (!activeSessionId) return;
    const session = sessions.find(item => item.id === activeSessionId);
    if (!session) return;
    loadedSessionIdRef.current = null;
    setters.question('');
    setters.messages(session.messages || []);
    setters.response(session.response || '');
    setters.deepResearch(Boolean(session.deepResearch));
    setters.researchStages(session.researchStages || []);
    setters.currentStageIndex(session.currentStageIndex || 0);
    setters.researchIteration(session.researchIteration || 0);
    setters.researchComplete(Boolean(session.researchComplete));
    setters.includeSecurityContext(Boolean(session.includeSecurityContext));
    setters.codeSessionId(session.codeSessionId);
    onRestoreCodeMode?.(Boolean(session.codeMode) && codeModeAvailable);
    const timer = window.setTimeout(() => {
      loadedSessionIdRef.current = activeSessionId;
    }, 0);
    return () => window.clearTimeout(timer);
    // Session content changes while streaming; restoration only belongs to
    // an explicit active-session change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId]);

  const {
    messages,
    response,
    deepResearch,
    researchStages,
    currentStageIndex,
    researchIteration,
    researchComplete,
    codeMode,
    includeSecurityContext,
    codeSessionId,
  } = snapshot;
  useEffect(() => {
    if (!activeSessionId || loadedSessionIdRef.current !== activeSessionId) {
      return;
    }
    setSessions(previous => previous.map(session =>
      session.id === activeSessionId
        ? {
            ...session,
            updatedAt: Date.now(),
            messages,
            response,
            deepResearch,
            researchStages,
            currentStageIndex,
            researchIteration,
            researchComplete,
            codeMode,
            includeSecurityContext,
            codeSessionId,
          }
        : session
    ));
  }, [
    activeSessionId,
    messages,
    response,
    deepResearch,
    researchStages,
    currentStageIndex,
    researchIteration,
    researchComplete,
    codeMode,
    includeSecurityContext,
    codeSessionId,
  ]);

  useEffect(() => {
    if (sessions.length === 0) return;
    try {
      localStorage.setItem(
        storageKey,
        JSON.stringify(
          [...sessions]
            .sort((left, right) => right.updatedAt - left.updatedAt)
            .slice(0, MAX_STORED_SESSIONS),
        ),
      );
    } catch (error) {
      console.error('Failed to persist chat sessions:', error);
    }
  }, [sessions, storageKey]);

  return {
    sessions,
    setSessions,
    activeSessionId,
    setActiveSessionId,
    createSession,
  };
}
