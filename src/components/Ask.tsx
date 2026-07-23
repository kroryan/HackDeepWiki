'use client';

import React, {useState, useRef, useEffect, useMemo} from 'react';
import {
  FaChevronLeft,
  FaChevronRight,
  FaPlus,
  FaTrash,
} from 'react-icons/fa';
import Markdown from './Markdown';
import { useLanguage } from '@/contexts/LanguageContext';
import RepoInfo from '@/types/repoinfo';
import getRepoUrl from '@/utils/getRepoUrl';
import ModelSelectionModal from './ModelSelectionModal';
import { createChatWebSocket, closeWebSocket, ChatCompletionRequest } from '@/utils/websocketClient';
import { getSavedApiCredentials } from '@/utils/apiCredentials';
import { StreamParser, ProcessEvent } from '@/utils/streamParser';

interface Model {
  id: string;
  name: string;
}

interface Provider {
  id: string;
  name: string;
  models: Model[];
  supportsCustomModel?: boolean;
}

interface Message {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

interface ResearchStage {
  title: string;
  content: string;
  iteration: number;
  type: 'plan' | 'update' | 'conclusion';
}

interface ChatSession {
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
}

interface AskProps {
  repoInfo: RepoInfo;
  provider?: string;
  model?: string;
  isCustomModel?: boolean;
  customModel?: string;
  language?: string;
  // Wiki page id (repo chat) or ZIM entry path the chat was opened from --
  // passed straight through to the backend, which scopes the initial
  // context to that page/entry plus a handful of related ones instead of
  // the whole repo/archive when it's set.
  currentPageId?: string;
  onRef?: (ref: { clearConversation: () => void }) => void;
}

const Ask: React.FC<AskProps> = ({
  repoInfo,
  provider = '',
  model = '',
  isCustomModel = false,
  customModel = '',
  language = 'en',
  currentPageId,
  onRef
}) => {
  const [question, setQuestion] = useState('');
  const [response, setResponse] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [deepResearch, setDeepResearch] = useState(false);
  // 🔐 When on, the latest saved Security Analysis / Website Security scan
  // report for this repo is summarized and injected into the prompt so the
  // LLM can answer questions about vulnerabilities directly. Off by default
  // -- most questions aren't about security, and the report can be sizable.
  const [includeSecurityContext, setIncludeSecurityContext] = useState(false);
  // Behind-the-scenes events (tool calls, reasoning tokens) for the CURRENT
  // in-flight/most recent answer, extracted from the stream by StreamParser
  // (see src/utils/streamParser.ts) -- shown in a collapsible panel rather
  // than persisted per history entry.
  const [processEvents, setProcessEvents] = useState<ProcessEvent[]>([]);
  const [showProcess, setShowProcess] = useState(false);

  // Model selection state
  const [selectedProvider, setSelectedProvider] = useState(provider);
  const [selectedModel, setSelectedModel] = useState(model);
  const [isCustomSelectedModel, setIsCustomSelectedModel] = useState(isCustomModel);
  const [customSelectedModel, setCustomSelectedModel] = useState(customModel);
  const [isModelSelectionModalOpen, setIsModelSelectionModalOpen] = useState(false);
  const [isComprehensiveView, setIsComprehensiveView] = useState(true);

  // Get language context for translations
  const { messages } = useLanguage();

  // Research navigation state
  const [researchStages, setResearchStages] = useState<ResearchStage[]>([]);
  const [currentStageIndex, setCurrentStageIndex] = useState(0);
  const [conversationHistory, setConversationHistory] = useState<Message[]>([]);
  const [researchIteration, setResearchIteration] = useState(0);
  const [researchComplete, setResearchComplete] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const responseRef = useRef<HTMLDivElement>(null);
  const providerRef = useRef(provider);
  const modelRef = useRef(model);
  const loadedSessionIdRef = useRef<string | null>(null);
  const storageKey = useMemo(
    () => `hackdeepwiki-chat-sessions:${repoInfo.type}:${repoInfo.owner}:${repoInfo.repo}`,
    [repoInfo.type, repoInfo.owner, repoInfo.repo],
  );
  const createSession = (title?: string): ChatSession => {
    const now = Date.now();
    return {
      id: `chat-${now}-${Math.random().toString(36).slice(2, 8)}`,
      title: title || (messages.ask?.newChat || 'New chat'),
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
  };
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState('');
  const [showHistory, setShowHistory] = useState(false);

  // Load chat sessions independently for each repository.
  useEffect(() => {
    loadedSessionIdRef.current = null;
    try {
      const stored = localStorage.getItem(storageKey);
      const parsed = stored ? JSON.parse(stored) as ChatSession[] : [];
      const valid = Array.isArray(parsed)
        ? parsed.filter(session => session && session.id && Array.isArray(session.messages))
        : [];
      const initialSessions = valid.length > 0 ? valid : [createSession()];
      setSessions(initialSessions);
      setActiveSessionId(initialSessions[0].id);
    } catch (error) {
      console.error('Failed to load chat sessions:', error);
      const initial = createSession();
      setSessions([initial]);
      setActiveSessionId(initial.id);
    }
    // createSession deliberately uses the current translated default title.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey]);

  // Restore the selected session without writing stale state into it.
  // Deliberately excludes `sessions`: the effect below (persist-as-it-streams)
  // writes conversationHistory/response back into `sessions` on every token,
  // so depending on `sessions` here would re-fire this restore on every
  // streamed chunk and stomp the in-progress response with what was just
  // persisted a moment earlier -- this should only run when the user
  // actually switches to a different session.
  useEffect(() => {
    if (!activeSessionId) return;
    const session = sessions.find(item => item.id === activeSessionId);
    if (!session) return;
    loadedSessionIdRef.current = null;
    setQuestion('');
    setConversationHistory(session.messages || []);
    setResponse(session.response || '');
    setDeepResearch(Boolean(session.deepResearch));
    setResearchStages(session.researchStages || []);
    setCurrentStageIndex(session.currentStageIndex || 0);
    setResearchIteration(session.researchIteration || 0);
    setResearchComplete(Boolean(session.researchComplete));
    const timer = window.setTimeout(() => {
      loadedSessionIdRef.current = activeSessionId;
    }, 0);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId]);

  // Persist the active conversation as it streams.
  useEffect(() => {
    if (
      !activeSessionId ||
      loadedSessionIdRef.current !== activeSessionId
    ) return;
    setSessions(previous => previous.map(session =>
      session.id === activeSessionId
        ? {
            ...session,
            updatedAt: Date.now(),
            messages: conversationHistory,
            response,
            deepResearch,
            researchStages,
            currentStageIndex,
            researchIteration,
            researchComplete,
          }
        : session
    ));
  }, [
    activeSessionId,
    conversationHistory,
    response,
    deepResearch,
    researchStages,
    currentStageIndex,
    researchIteration,
    researchComplete,
  ]);

  useEffect(() => {
    if (sessions.length === 0) return;
    try {
      localStorage.setItem(
        storageKey,
        JSON.stringify(
          [...sessions]
            .sort((a, b) => b.updatedAt - a.updatedAt)
            .slice(0, 20),
        ),
      );
    } catch (error) {
      console.error('Failed to persist chat sessions:', error);
    }
  }, [sessions, storageKey]);

  // Focus input on component mount
  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.focus();
    }
  }, []);

  // Expose clearConversation method to parent component
  useEffect(() => {
    if (onRef) {
      onRef({ clearConversation });
    }
  }, [onRef]);

  // Scroll to bottom of response when it changes
  useEffect(() => {
    if (responseRef.current) {
      responseRef.current.scrollTop = responseRef.current.scrollHeight;
    }
  }, [response, conversationHistory]);

  // Close WebSocket when component unmounts
  useEffect(() => {
    return () => {
      closeWebSocket(webSocketRef.current);
    };
  }, []);

  useEffect(() => {
    providerRef.current = provider;
    modelRef.current = model;
  }, [provider, model]);

  useEffect(() => {
    const fetchModel = async () => {
      try {
        setIsLoading(true);

        const response = await fetch('/api/models/config', { cache: 'no-store' });
        if (!response.ok) {
          throw new Error(`Error fetching model configurations: ${response.status}`);
        }

        const data = await response.json();

        // use latest provider/model ref to check
        if(providerRef.current == '' || modelRef.current== '') {
          setSelectedProvider(data.defaultProvider);

          // Find the default provider and set its default model
          const selectedProvider = data.providers.find((p:Provider) => p.id === data.defaultProvider);
          if (selectedProvider && selectedProvider.models.length > 0) {
            setSelectedModel(selectedProvider.models[0].id);
          }
        } else {
          setSelectedProvider(providerRef.current);
          setSelectedModel(modelRef.current);
        }
      } catch (err) {
        console.error('Failed to fetch model configurations:', err);
      } finally {
        setIsLoading(false);
      }
    };
    if(provider == '' || model == '') {
      fetchModel()
    }
  }, [provider, model]);

  const clearConversation = () => {
    closeWebSocket(webSocketRef.current);
    setQuestion('');
    setResponse('');
    setProcessEvents([]);
    setConversationHistory([]);
    setResearchIteration(0);
    setResearchComplete(false);
    setResearchStages([]);
    setCurrentStageIndex(0);
    if (inputRef.current) {
      inputRef.current.focus();
    }
  };

  const startNewChat = () => {
    if (isLoading) return;
    const session = createSession();
    setSessions(previous => [session, ...previous]);
    setActiveSessionId(session.id);
  };

  const selectSession = (sessionId: string) => {
    if (isLoading || sessionId === activeSessionId) return;
    closeWebSocket(webSocketRef.current);
    setActiveSessionId(sessionId);
  };

  const deleteActiveChat = (sessionId?: string) => {
    const targetId = sessionId || activeSessionId;
    if (isLoading || !targetId) return;
    if (!window.confirm(
      messages.ask?.deleteChatConfirm ||
      'Delete this chat and its complete history?',
    )) return;

    const remaining = sessions.filter(session => session.id !== targetId);
    if (remaining.length > 0) {
      setSessions(remaining);
      setActiveSessionId(remaining[0].id);
    } else {
      const replacement = createSession();
      setSessions([replacement]);
      setActiveSessionId(replacement.id);
    }
  };

  const downloadresponse = () =>{
  const transcript = [
    ...conversationHistory,
    ...(response ? [{role: 'assistant' as const, content: response}] : []),
  ]
    .filter(message => message.content !== '[DEEP RESEARCH] Continue the research')
    .map(message => `## ${message.role === 'user'
      ? (messages.ask?.you || 'You')
      : (messages.ask?.assistant || 'Assistant')}\n\n${message.content}`)
    .join('\n\n');
  const blob = new Blob([transcript], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `response-${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

  // Function to check if research is complete based on response content
  const checkIfResearchComplete = (content: string): boolean => {
    // Check for explicit final conclusion markers
    if (content.includes('## Final Conclusion')) {
      return true;
    }

    // Check for conclusion sections that don't indicate further research
    if ((content.includes('## Conclusion') || content.includes('## Summary')) &&
      !content.includes('I will now proceed to') &&
      !content.includes('Next Steps') &&
      !content.includes('next iteration')) {
      return true;
    }

    // Check for phrases that explicitly indicate completion
    if (content.includes('This concludes our research') ||
      content.includes('This completes our investigation') ||
      content.includes('This concludes the deep research process') ||
      content.includes('Key Findings and Implementation Details') ||
      content.includes('In conclusion,') ||
      (content.includes('Final') && content.includes('Conclusion'))) {
      return true;
    }

    // Check for topic-specific completion indicators
    if (content.includes('Dockerfile') &&
      (content.includes('This Dockerfile') || content.includes('The Dockerfile')) &&
      !content.includes('Next Steps') &&
      !content.includes('In the next iteration')) {
      return true;
    }

    return false;
  };

  // Function to extract research stages from the response
  const extractResearchStage = (content: string, iteration: number): ResearchStage | null => {
    // Check for research plan (first iteration)
    if (iteration === 1 && content.includes('## Research Plan')) {
      const planMatch = content.match(/## Research Plan([\s\S]*?)(?:## Next Steps|$)/);
      if (planMatch) {
        return {
          title: 'Research Plan',
          content: content,
          iteration: 1,
          type: 'plan'
        };
      }
    }

    // Check for research updates (iterations 1-4)
    if (iteration >= 1 && iteration <= 4) {
      const updateMatch = content.match(new RegExp(`## Research Update ${iteration}([\\s\\S]*?)(?:## Next Steps|$)`));
      if (updateMatch) {
        return {
          title: `Research Update ${iteration}`,
          content: content,
          iteration: iteration,
          type: 'update'
        };
      }
    }

    // Check for final conclusion
    if (content.includes('## Final Conclusion')) {
      const conclusionMatch = content.match(/## Final Conclusion([\s\S]*?)$/);
      if (conclusionMatch) {
        return {
          title: 'Final Conclusion',
          content: content,
          iteration: iteration,
          type: 'conclusion'
        };
      }
    }

    return null;
  };

  // Function to navigate to a specific research stage
  const navigateToStage = (index: number) => {
    if (index >= 0 && index < researchStages.length) {
      setCurrentStageIndex(index);
      setResponse(researchStages[index].content);
    }
  };

  // Function to navigate to the next research stage
  const navigateToNextStage = () => {
    if (currentStageIndex < researchStages.length - 1) {
      navigateToStage(currentStageIndex + 1);
    }
  };

  // Function to navigate to the previous research stage
  const navigateToPreviousStage = () => {
    if (currentStageIndex > 0) {
      navigateToStage(currentStageIndex - 1);
    }
  };

  // WebSocket reference
  const webSocketRef = useRef<WebSocket | null>(null);

  // Function to continue research automatically
  const continueResearch = async () => {
    if (!deepResearch || researchComplete || !response || isLoading) return;

    // Add a small delay to allow the user to read the current response
    await new Promise(resolve => setTimeout(resolve, 2000));

    setIsLoading(true);

    try {
      // Store the current response for use in the history
      const currentResponse = response;

      // Create a new message from the AI's previous response
      const newHistory: Message[] = [
        ...conversationHistory,
        {
          role: 'assistant',
          content: currentResponse
        },
        {
          role: 'user',
          content: '[DEEP RESEARCH] Continue the research'
        }
      ];

      // Update conversation history
      setConversationHistory(newHistory);

      // Increment research iteration
      const newIteration = researchIteration + 1;
      setResearchIteration(newIteration);

      // Clear previous response
      setResponse('');
      setProcessEvents([]);

      // Prepare the request body
      const requestBody: ChatCompletionRequest = {
        repo_url: getRepoUrl(repoInfo),
        type: repoInfo.type,
        current_page_id: currentPageId,
        messages: newHistory.map(msg => ({ role: msg.role as 'user' | 'assistant', content: msg.content })),
        provider: selectedProvider,
        model: isCustomSelectedModel ? customSelectedModel : selectedModel,
        language: language,
        include_security_context: includeSecurityContext,
        owner: repoInfo.owner,
        repo: repoInfo.repo,
        ...getSavedApiCredentials(selectedProvider)
      };

      // Add tokens if available
      if (repoInfo?.token) {
        requestBody.token = repoInfo.token;
      }

      // Close any existing WebSocket connection
      closeWebSocket(webSocketRef.current);

      let fullResponse = '';
      const streamParser = new StreamParser();

      // Create a new WebSocket connection
      webSocketRef.current = createChatWebSocket(
        requestBody,
        // Message handler
        (message: string) => {
          const { text, events } = streamParser.feed(message);
          if (events.length > 0) {
            setProcessEvents(prev => [...prev, ...events]);
          }
          fullResponse += text;
          setResponse(fullResponse);

          // Extract research stage if this is a deep research response
          if (deepResearch) {
            const stage = extractResearchStage(fullResponse, newIteration);
            if (stage) {
              // Add the stage to the research stages if it's not already there
              setResearchStages(prev => {
                // Check if we already have this stage
                const existingStageIndex = prev.findIndex(s => s.iteration === stage.iteration && s.type === stage.type);
                if (existingStageIndex >= 0) {
                  // Update existing stage
                  const newStages = [...prev];
                  newStages[existingStageIndex] = stage;
                  return newStages;
                } else {
                  // Add new stage
                  return [...prev, stage];
                }
              });

              // Update current stage index to the latest stage
              setCurrentStageIndex(researchStages.length);
            }
          }
        },
        // Error handler
        (error: Event) => {
          console.error('WebSocket error:', error);
          setResponse(prev => prev + '\n\nError: WebSocket connection failed. Falling back to HTTP...');

          // Fallback to HTTP if WebSocket fails
          void fallbackToHttp(requestBody, newHistory);
        },
        // Close handler
        () => {
          // Check if research is complete when the WebSocket closes
          const isComplete = checkIfResearchComplete(fullResponse);

          // Force completion after a maximum number of iterations (5)
          const forceComplete = newIteration >= 5;

          if (forceComplete && !isComplete) {
            // If we're forcing completion, append a comprehensive conclusion to the response
            const completionNote = "\n\n## Final Conclusion\nAfter multiple iterations of deep research, we've gathered significant insights about this topic. This concludes our investigation process, having reached the maximum number of research iterations. The findings presented across all iterations collectively form our comprehensive answer to the original question.";
            fullResponse += completionNote;
            setResponse(fullResponse);
            setResearchComplete(true);
          } else {
            setResearchComplete(isComplete);
          }

          setIsLoading(false);
        }
      );
    } catch (error) {
      console.error('Error during API call:', error);
      setResponse(prev => prev + '\n\nError: Failed to continue research. Please try again.');
      setResearchComplete(true);
      setIsLoading(false);
    }
  };

  // Fallback to HTTP if WebSocket fails
  const fallbackToHttp = async (
    requestBody: ChatCompletionRequest,
    historyForRequest: Message[],
  ) => {
    try {
      // Make the API call using HTTP
      const apiResponse = await fetch(`/api/chat/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestBody)
      });

      if (!apiResponse.ok) {
        throw new Error(`API error: ${apiResponse.status}`);
      }

      // Process the streaming response
      const reader = apiResponse.body?.getReader();
      const decoder = new TextDecoder();

      if (!reader) {
        throw new Error('Failed to get response reader');
      }

      // Read the stream
      let fullResponse = '';
      const streamParser = new StreamParser();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        const { text, events } = streamParser.feed(chunk);
        if (events.length > 0) {
          setProcessEvents(prev => [...prev, ...events]);
        }
        fullResponse += text;
        setResponse(fullResponse);

        // Extract research stage if this is a deep research response
        if (deepResearch) {
          const stage = extractResearchStage(fullResponse, researchIteration);
          if (stage) {
            // Add the stage to the research stages
            setResearchStages(prev => {
              const existingStageIndex = prev.findIndex(s => s.iteration === stage.iteration && s.type === stage.type);
              if (existingStageIndex >= 0) {
                const newStages = [...prev];
                newStages[existingStageIndex] = stage;
                return newStages;
              } else {
                return [...prev, stage];
              }
            });
          }
        }
      }

      // Check if research is complete
      const isComplete = checkIfResearchComplete(fullResponse);

      // Force completion after a maximum number of iterations (5)
      const forceComplete = researchIteration >= 5;

      if (forceComplete && !isComplete) {
        // If we're forcing completion, append a comprehensive conclusion to the response
        const completionNote = "\n\n## Final Conclusion\nAfter multiple iterations of deep research, we've gathered significant insights about this topic. This concludes our investigation process, having reached the maximum number of research iterations. The findings presented across all iterations collectively form our comprehensive answer to the original question.";
        fullResponse += completionNote;
        setResponse(fullResponse);
        setResearchComplete(true);
      } else {
        setResearchComplete(isComplete);
      }
      if (!deepResearch && fullResponse) {
        setConversationHistory([
          ...historyForRequest,
          { role: 'assistant', content: fullResponse },
        ]);
        setResponse('');
        setProcessEvents([]);
      }
    } catch (error) {
      console.error('Error during HTTP fallback:', error);
      setResponse(prev => prev + '\n\nError: Failed to get a response. Please try again.');
      setResearchComplete(true);
    } finally {
      setIsLoading(false);
    }
  };

  // Effect to continue research when response is updated
  useEffect(() => {
    if (deepResearch && response && !isLoading && !researchComplete) {
      const isComplete = checkIfResearchComplete(response);
      if (isComplete) {
        setResearchComplete(true);
      } else if (researchIteration > 0 && researchIteration < 5) {
        // Only auto-continue if we're already in a research process and haven't reached max iterations
        // Use setTimeout to avoid potential infinite loops
        const timer = setTimeout(() => {
          continueResearch();
        }, 1000);
        return () => clearTimeout(timer);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [response, isLoading, deepResearch, researchComplete, researchIteration]);

  // Effect to update research stages when the response changes
  useEffect(() => {
    if (deepResearch && response && !isLoading) {
      // Try to extract a research stage from the response
      const stage = extractResearchStage(response, researchIteration);
      if (stage) {
        // Add or update the stage in the research stages
        setResearchStages(prev => {
          // Check if we already have this stage
          const existingStageIndex = prev.findIndex(s => s.iteration === stage.iteration && s.type === stage.type);
          if (existingStageIndex >= 0) {
            // Update existing stage
            const newStages = [...prev];
            newStages[existingStageIndex] = stage;
            return newStages;
          } else {
            // Add new stage
            return [...prev, stage];
          }
        });

        // Update current stage index to point to this stage
        setCurrentStageIndex(prev => {
          const newIndex = researchStages.findIndex(s => s.iteration === stage.iteration && s.type === stage.type);
          return newIndex >= 0 ? newIndex : prev;
        });
      }
    }

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [response, isLoading, deepResearch, researchIteration]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!question.trim() || isLoading) return;

    handleConfirmAsk();
  };

  // Handle confirm and send request
  const handleConfirmAsk = async () => {
    setIsLoading(true);
    setResponse('');
    setProcessEvents([]);
    setResearchIteration(0);
    setResearchComplete(false);

    try {
      // Create initial message
      const initialMessage: Message = {
        role: 'user',
        content: deepResearch ? `[DEEP RESEARCH] ${question}` : question
      };

      // Set initial conversation history
      const newHistory: Message[] = [...conversationHistory, initialMessage];
      setConversationHistory(newHistory);
      setQuestion('');
      setSessions(previous => previous.map(session => {
        if (session.id !== activeSessionId || session.messages.length > 0) {
          return session;
        }
        const cleanQuestion = question.trim().replace(/^\[DEEP RESEARCH\]\s*/i, '');
        return {
          ...session,
          title: cleanQuestion.slice(0, 48) || session.title,
          updatedAt: Date.now(),
        };
      }));

      // Prepare request body
      const requestBody: ChatCompletionRequest = {
        repo_url: getRepoUrl(repoInfo),
        type: repoInfo.type,
        current_page_id: currentPageId,
        messages: newHistory.map(msg => ({ role: msg.role as 'user' | 'assistant', content: msg.content })),
        provider: selectedProvider,
        model: isCustomSelectedModel ? customSelectedModel : selectedModel,
        language: language,
        include_security_context: includeSecurityContext,
        owner: repoInfo.owner,
        repo: repoInfo.repo,
        ...getSavedApiCredentials(selectedProvider)
      };

      // Add tokens if available
      if (repoInfo?.token) {
        requestBody.token = repoInfo.token;
      }

      // Close any existing WebSocket connection
      closeWebSocket(webSocketRef.current);

      let fullResponse = '';
      const streamParser = new StreamParser();

      // Create a new WebSocket connection
      let usedHttpFallback = false;
      webSocketRef.current = createChatWebSocket(
        requestBody,
        // Message handler
        (message: string) => {
          const { text, events } = streamParser.feed(message);
          if (events.length > 0) {
            setProcessEvents(prev => [...prev, ...events]);
          }
          fullResponse += text;
          setResponse(fullResponse);

          // Extract research stage if this is a deep research response
          if (deepResearch) {
            const stage = extractResearchStage(fullResponse, 1); // First iteration
            if (stage) {
              // Add the stage to the research stages
              setResearchStages([stage]);
              setCurrentStageIndex(0);
            }
          }
        },
        // Error handler
        (error: Event) => {
          console.error('WebSocket error:', error);
          if (usedHttpFallback) return;
          usedHttpFallback = true;
          setResponse('');
          setProcessEvents([]);
          void fallbackToHttp(requestBody, newHistory);
        },
        // Close handler
        () => {
          if (usedHttpFallback) return;
          // If deep research is enabled, check if we should continue
          if (deepResearch) {
            const isComplete = checkIfResearchComplete(fullResponse);
            setResearchComplete(isComplete);

            // If not complete, start the research process
            if (!isComplete) {
              setResearchIteration(1);
              // The continueResearch function will be triggered by the useEffect
            }
          } else if (fullResponse) {
            setConversationHistory([
              ...newHistory,
              { role: 'assistant', content: fullResponse },
            ]);
            setResponse('');
            setProcessEvents([]);
          }

          setIsLoading(false);
        }
      );
    } catch (error) {
      console.error('Error during API call:', error);
      setResponse(prev => prev + '\n\nError: Failed to get a response. Please try again.');
      setResearchComplete(true);
      setIsLoading(false);
    }
  };

  const [buttonWidth, setButtonWidth] = useState(0);
  const buttonRef = useRef<HTMLButtonElement>(null);

  // Measure button width and update state
  useEffect(() => {
    if (buttonRef.current) {
      const width = buttonRef.current.offsetWidth;
      setButtonWidth(width);
    }
  }, [messages.ask?.askButton, isLoading]);

  return (
    <div>
      <div className="p-4">
        <div className="flex items-center justify-between mb-3 gap-2">
          <div className="flex items-center gap-1.5">
            {/* New chat */}
            <button
              type="button"
              onClick={startNewChat}
              disabled={isLoading}
              title={messages.ask?.newChat || 'New chat'}
              className="text-xs px-2.5 py-1 rounded border border-[var(--border-color)]/40 bg-[var(--background)]/10 text-[var(--foreground)]/80 hover:bg-[var(--background)]/30 hover:text-[var(--foreground)] transition-colors flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <FaPlus className="h-3 w-3" />
              <span className="hidden sm:inline">{messages.ask?.newChat || 'New chat'}</span>
            </button>
            {/* History toggle */}
            <button
              type="button"
              onClick={() => setShowHistory(previous => !previous)}
              title={messages.ask?.chatHistory || 'Chat history'}
              className={`text-xs px-2.5 py-1 rounded border transition-colors flex items-center gap-1.5 ${
                showHistory
                  ? 'border-[var(--accent-primary)]/30 bg-[var(--accent-primary)]/10 text-[var(--accent-primary)]'
                  : 'border-[var(--border-color)]/40 bg-[var(--background)]/10 text-[var(--foreground)]/80 hover:bg-[var(--background)]/30'
              }`}
            >
              <FaChevronRight className={`h-3 w-3 transition-transform ${showHistory ? 'rotate-90' : ''}`} />
              <span className="hidden sm:inline">{messages.ask?.chatHistory || 'Chat history'}</span>
            </button>
          </div>

          {/* Model selection button */}
          <button
            type="button"
            onClick={() => setIsModelSelectionModalOpen(true)}
            className="text-xs px-2.5 py-1 rounded border border-[var(--border-color)]/40 bg-[var(--background)]/10 text-[var(--foreground)]/80 hover:bg-[var(--background)]/30 hover:text-[var(--foreground)] transition-colors flex items-center gap-1.5"
          >
            <span className="truncate max-w-[160px]">{selectedProvider}/{isCustomSelectedModel ? customSelectedModel : selectedModel}</span>
            <svg className="h-3.5 w-3.5 text-[var(--accent-primary)]/70" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
            </svg>
          </button>
        </div>

        {/* Chat history / session list */}
        {showHistory && (
          <div className="mb-3 rounded-md border border-[var(--border-color)] bg-[var(--background)]/40 max-h-56 overflow-y-auto">
            {sessions.length === 0 ? (
              <p className="p-3 text-xs text-[var(--muted)]">
                {messages.ask?.emptyConversation || 'Ask a question to start this conversation.'}
              </p>
            ) : (
              <ul className="divide-y divide-[var(--border-color)]/60">
                {sessions.map(session => (
                  <li
                    key={session.id}
                    className={`flex items-center gap-2 px-3 py-2 cursor-pointer transition-colors ${
                      session.id === activeSessionId
                        ? 'bg-[var(--accent-primary)]/10'
                        : 'hover:bg-[var(--background)]/30'
                    }`}
                    onClick={() => selectSession(session.id)}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-xs font-medium text-[var(--foreground)]">
                        {session.title || (messages.ask?.newChat || 'New chat')}
                      </div>
                      <div className="truncate text-[10px] text-[var(--muted)]">
                        {session.messages.length > 0
                          ? `${session.messages.length} ${messages.ask?.messages || 'messages'}`
                          : (messages.ask?.emptyConversation || 'Ask a question to start this conversation.')}
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        deleteActiveChat(session.id);
                      }}
                      disabled={isLoading}
                      title={messages.ask?.deleteChat || 'Delete chat'}
                      className="text-[var(--muted)] hover:text-red-500 transition-colors p-1 disabled:opacity-50"
                    >
                      <FaTrash className="h-3 w-3" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {/* Question input */}
        <form onSubmit={handleSubmit} className="mt-4">
          <div className="relative">
            <input
              ref={inputRef}
              type="text"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder={messages.ask?.placeholder || 'What would you like to know about this codebase?'}
              className="block w-full rounded-md border border-[var(--border-color)] bg-[var(--input-bg)] text-[var(--foreground)] px-5 py-3.5 text-base shadow-sm focus:border-[var(--accent-primary)] focus:ring-2 focus:ring-[var(--accent-primary)]/30 focus:outline-none transition-all"
              style={{ paddingRight: `${buttonWidth + 24}px` }}
              disabled={isLoading}
            />
            <button
              ref={buttonRef}
              type="submit"
              disabled={isLoading || !question.trim()}
              className={`absolute right-3 top-1/2 transform -translate-y-1/2 px-4 py-2 rounded-md font-medium text-sm ${
                isLoading || !question.trim()
                  ? 'bg-[var(--button-disabled-bg)] text-[var(--button-disabled-text)] cursor-not-allowed'
                  : 'bg-[var(--accent-primary)] text-white hover:bg-[var(--accent-primary)]/90 shadow-sm'
              } transition-all duration-200 flex items-center gap-1.5`}
            >
              {isLoading ? (
                <div className="w-4 h-4 rounded-full border-2 border-t-transparent border-white animate-spin" />
              ) : (
                <>
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 5l7 7-7 7M5 5l7 7-7 7" />
                  </svg>
                  <span>{messages.ask?.askButton || 'Ask'}</span>
                </>
              )}
            </button>
          </div>

          {/* Deep Research / Security context toggles */}
          <div className="flex items-center mt-2 justify-between flex-wrap gap-y-2">
            <div className="flex items-center gap-4">
              <div className="group relative">
                <label className="flex items-center cursor-pointer">
                  <span className="text-xs text-[var(--muted)] mr-2">Deep Research</span>
                  <div className="relative">
                    <input
                      type="checkbox"
                      checked={deepResearch}
                      onChange={() => setDeepResearch(!deepResearch)}
                      className="sr-only"
                    />
                    <div className={`w-10 h-5 rounded-full transition-colors ${deepResearch ? 'bg-[var(--accent-primary)]' : 'bg-[var(--muted)]/30'}`}></div>
                    <div className={`absolute left-0.5 top-0.5 w-4 h-4 rounded-full bg-white transition-transform transform ${deepResearch ? 'translate-x-5' : ''}`}></div>
                  </div>
                </label>
                <div className="absolute bottom-full left-0 mb-2 hidden group-hover:block bg-[var(--card-bg)] text-[var(--foreground)] border border-[var(--border-color)] text-xs rounded p-2 w-72 z-10 shadow-lg">
                  <div className="relative">
                    <div className="absolute -bottom-2 left-4 w-0 h-0 border-l-4 border-r-4 border-t-4 border-transparent border-t-[var(--card-bg)]"></div>
                    <p className="mb-1">Deep Research conducts a multi-turn investigation process:</p>
                    <ul className="list-disc pl-4 text-xs">
                      <li><strong>Initial Research:</strong> Creates a research plan and initial findings</li>
                      <li><strong>Iteration 1:</strong> Explores specific aspects in depth</li>
                      <li><strong>Iteration 2:</strong> Investigates remaining questions</li>
                      <li><strong>Iterations 3-4:</strong> Dives deeper into complex areas</li>
                      <li><strong>Final Conclusion:</strong> Comprehensive answer based on all iterations</li>
                    </ul>
                    <p className="mt-1 text-xs italic">The AI automatically continues research until complete (up to 5 iterations)</p>
                  </div>
                </div>
              </div>

              <div className="group relative">
                <label className="flex items-center cursor-pointer">
                  <span className="text-xs text-[var(--muted)] mr-2">🔐 Security context</span>
                  <div className="relative">
                    <input
                      type="checkbox"
                      checked={includeSecurityContext}
                      onChange={() => setIncludeSecurityContext(!includeSecurityContext)}
                      className="sr-only"
                    />
                    <div className={`w-10 h-5 rounded-full transition-colors ${includeSecurityContext ? 'bg-[var(--accent-primary)]' : 'bg-[var(--muted)]/30'}`}></div>
                    <div className={`absolute left-0.5 top-0.5 w-4 h-4 rounded-full bg-white transition-transform transform ${includeSecurityContext ? 'translate-x-5' : ''}`}></div>
                  </div>
                </label>
                <div className="absolute bottom-full left-0 mb-2 hidden group-hover:block bg-[var(--card-bg)] text-[var(--foreground)] border border-[var(--border-color)] text-xs rounded p-2 w-72 z-10 shadow-lg">
                  <div className="relative">
                    <div className="absolute -bottom-2 left-4 w-0 h-0 border-l-4 border-r-4 border-t-4 border-transparent border-t-[var(--card-bg)]"></div>
                    <p>
                      Gives the AI the latest saved Security Analysis (dependency CVEs) and/or Website
                      Security scan report for this repo, so it can answer questions about vulnerabilities
                      directly. Never triggers a new scan -- if none has been run yet, this has no effect.
                    </p>
                  </div>
                </div>
              </div>
            </div>
            {deepResearch && (
              <div className="text-xs text-[var(--accent-primary)]">
                Multi-turn research process enabled
                {researchIteration > 0 && !researchComplete && ` (iteration ${researchIteration})`}
                {researchComplete && ` (complete)`}
              </div>
            )}
          </div>
        </form>

        {/* Conversation transcript and response area */}
        {(conversationHistory.length > 0 || response) && (
          <div className="border-t border-[var(--border-color)] mt-4">
            <div
              ref={responseRef}
              className="p-4 max-h-[45vh] overflow-y-auto space-y-4"
            >
              {conversationHistory.map((message, index) => {
                if (message.content === '[DEEP RESEARCH] Continue the research') {
                  return null;
                }
                const isUser = message.role === 'user';
                return (
                  <div key={`msg-${index}`} className="space-y-1">
                    <div className="text-xs font-medium text-[var(--muted)]">
                      {isUser
                        ? (messages.ask?.you || 'You')
                        : (messages.ask?.assistant || 'Assistant')}
                    </div>
                    <Markdown content={message.content} repoInfo={repoInfo} />
                  </div>
                );
              })}
              {processEvents.length > 0 && (
                <div className="rounded-md border border-[var(--border-color)]/60 bg-[var(--background)]/30 text-xs">
                  <button
                    type="button"
                    onClick={() => setShowProcess(previous => !previous)}
                    className="w-full flex items-center gap-1.5 px-2.5 py-1.5 text-[var(--muted)] hover:text-[var(--foreground)] transition-colors"
                  >
                    <FaChevronRight className={`h-2.5 w-2.5 transition-transform ${showProcess ? 'rotate-90' : ''}`} />
                    <span>
                      {messages.ask?.process || 'Process'} ({processEvents.length})
                    </span>
                  </button>
                  {showProcess && (
                    <ul className="px-2.5 pb-2 space-y-1.5 border-t border-[var(--border-color)]/60 pt-1.5">
                      {processEvents.map((event, index) => (
                        <li key={index} className="text-[var(--muted)]">
                          {event.kind === 'tool' ? (
                            <span>
                              <span className="text-[var(--accent-primary)]">{String(event.payload.label || 'Tool')}:</span>{' '}
                              {String(event.payload.query ?? '')}
                            </span>
                          ) : (
                            <span className="italic whitespace-pre-wrap">{String(event.payload.text ?? '')}</span>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              {response && <Markdown content={response} repoInfo={repoInfo} />}
            </div>

            {/* Research navigation and clear button */}
            <div className="p-2 flex justify-between items-center border-t border-[var(--border-color)]">
              {/* Research navigation */}
              {deepResearch && researchStages.length > 1 && (
                <div className="flex items-center space-x-2">
                  <button
                    onClick={() => navigateToPreviousStage()}
                    disabled={currentStageIndex === 0}
                    className={`p-1 rounded-md ${currentStageIndex === 0 ? 'text-[var(--muted)]/40' : 'text-[var(--muted)] hover:bg-[var(--accent-primary)]/10 hover:text-[var(--accent-primary)]'}`}
                    aria-label="Previous stage"
                  >
                    <FaChevronLeft size={12} />
                  </button>

                  <div className="text-xs text-[var(--muted)]">
                    {currentStageIndex + 1} / {researchStages.length}
                  </div>

                  <button
                    onClick={() => navigateToNextStage()}
                    disabled={currentStageIndex === researchStages.length - 1}
                    className={`p-1 rounded-md ${currentStageIndex === researchStages.length - 1 ? 'text-[var(--muted)]/40' : 'text-[var(--muted)] hover:bg-[var(--accent-primary)]/10 hover:text-[var(--accent-primary)]'}`}
                    aria-label="Next stage"
                  >
                    <FaChevronRight size={12} />
                  </button>

                  <div className="text-xs text-[var(--muted)] ml-2">
                    {researchStages[currentStageIndex]?.title || `Stage ${currentStageIndex + 1}`}
                  </div>
                </div>
              )}

            <div className="flex items-center space-x-2">
              {/* Download button */}
              <button
                onClick={downloadresponse}
                className="text-xs text-[var(--muted)] hover:text-[var(--highlight)] px-2 py-1 rounded-md hover:bg-[var(--accent-primary)]/10 flex items-center gap-1"
                title="Download response as markdown file"
              >
                <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
                Download
              </button>

              {/* Clear button */}
              <button
                id="ask-clear-conversation"
                onClick={clearConversation}
                className="text-xs text-[var(--muted)] hover:text-[var(--accent-primary)] px-2 py-1 rounded-md hover:bg-[var(--accent-primary)]/10"
              >
                Clear conversation
              </button>
            </div>
              </div>
          </div>
        )}

        {/* Loading indicator */}
        {isLoading && !response && (
          <div className="p-4 border-t border-[var(--border-color)]">
            <div className="flex items-center space-x-2">
              <div className="animate-pulse flex space-x-1">
                <div className="h-2 w-2 bg-[var(--accent-primary)] rounded-full"></div>
                <div className="h-2 w-2 bg-[var(--accent-primary)] rounded-full"></div>
                <div className="h-2 w-2 bg-[var(--accent-primary)] rounded-full"></div>
              </div>
              <span className="text-xs text-[var(--muted)]">
                {deepResearch
                  ? (researchIteration === 0
                    ? "Planning research approach..."
                    : `Research iteration ${researchIteration} in progress...`)
                  : "Thinking..."}
              </span>
            </div>
            {deepResearch && (
              <div className="mt-2 text-xs text-[var(--muted)] pl-5">
                <div className="flex flex-col space-y-1">
                  {researchIteration === 0 && (
                    <>
                      <div className="flex items-center">
                        <div className="w-2 h-2 bg-blue-500 rounded-full mr-2"></div>
                        <span>Creating research plan...</span>
                      </div>
                      <div className="flex items-center">
                        <div className="w-2 h-2 bg-green-500 rounded-full mr-2"></div>
                        <span>Identifying key areas to investigate...</span>
                      </div>
                    </>
                  )}
                  {researchIteration === 1 && (
                    <>
                      <div className="flex items-center">
                        <div className="w-2 h-2 bg-blue-500 rounded-full mr-2"></div>
                        <span>Exploring first research area in depth...</span>
                      </div>
                      <div className="flex items-center">
                        <div className="w-2 h-2 bg-green-500 rounded-full mr-2"></div>
                        <span>Analyzing code patterns and structures...</span>
                      </div>
                    </>
                  )}
                  {researchIteration === 2 && (
                    <>
                      <div className="flex items-center">
                        <div className="w-2 h-2 bg-amber-500 rounded-full mr-2"></div>
                        <span>Investigating remaining questions...</span>
                      </div>
                      <div className="flex items-center">
                        <div className="w-2 h-2 bg-[var(--accent-primary)] rounded-full mr-2"></div>
                        <span>Connecting findings from previous iterations...</span>
                      </div>
                    </>
                  )}
                  {researchIteration === 3 && (
                    <>
                      <div className="flex items-center">
                        <div className="w-2 h-2 bg-indigo-500 rounded-full mr-2"></div>
                        <span>Exploring deeper connections...</span>
                      </div>
                      <div className="flex items-center">
                        <div className="w-2 h-2 bg-blue-500 rounded-full mr-2"></div>
                        <span>Analyzing complex patterns...</span>
                      </div>
                    </>
                  )}
                  {researchIteration === 4 && (
                    <>
                      <div className="flex items-center">
                        <div className="w-2 h-2 bg-teal-500 rounded-full mr-2"></div>
                        <span>Refining research conclusions...</span>
                      </div>
                      <div className="flex items-center">
                        <div className="w-2 h-2 bg-cyan-500 rounded-full mr-2"></div>
                        <span>Addressing remaining edge cases...</span>
                      </div>
                    </>
                  )}
                  {researchIteration >= 5 && (
                    <>
                      <div className="flex items-center">
                        <div className="w-2 h-2 bg-[var(--accent-primary)] rounded-full mr-2"></div>
                        <span>Finalizing comprehensive answer...</span>
                      </div>
                      <div className="flex items-center">
                        <div className="w-2 h-2 bg-green-500 rounded-full mr-2"></div>
                        <span>Synthesizing all research findings...</span>
                      </div>
                    </>
                  )}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Empty state */}
        {!isLoading && conversationHistory.length === 0 && !response && (
          <div className="mt-6 text-center text-sm text-[var(--muted)]">
            {messages.ask?.emptyConversation || 'Ask a question to start this conversation.'}
          </div>
        )}
      </div>
      <ModelSelectionModal
        isOpen={isModelSelectionModalOpen}
        onClose={() => setIsModelSelectionModalOpen(false)}
        provider={selectedProvider}
        setProvider={setSelectedProvider}
        model={selectedModel}
        setModel={setSelectedModel}
        isCustomModel={isCustomSelectedModel}
        setIsCustomModel={setIsCustomSelectedModel}
        customModel={customSelectedModel}
        setCustomModel={setCustomSelectedModel}
        isComprehensiveView={isComprehensiveView}
        setIsComprehensiveView={setIsComprehensiveView}
        showFileFilters={false}
        onApply={() => {
          console.log('Model selection applied:', selectedProvider, selectedModel);
        }}
        showWikiType={false}
        authRequired={false}
        isAuthLoading={false}
      />
    </div>
  );
};

export default Ask;
