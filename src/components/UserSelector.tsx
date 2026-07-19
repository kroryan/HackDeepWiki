'use client';

import React, { useState, useEffect } from 'react';
import { useLanguage } from '@/contexts/LanguageContext';

// Define the interfaces for our model configuration
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

interface ModelConfig {
  providers: Provider[];
  defaultProvider: string;
}

interface ModelSelectorProps {
  provider: string;
  setProvider: (value: string) => void;
  model: string;
  setModel: (value: string) => void;
  isCustomModel: boolean;
  setIsCustomModel: (value: boolean) => void;
  customModel: string;
  setCustomModel: (value: string) => void;

  // File filter configuration
  showFileFilters?: boolean;
  excludedDirs?: string;
  setExcludedDirs?: (value: string) => void;
  excludedFiles?: string;
  setExcludedFiles?: (value: string) => void;
  includedDirs?: string;
  setIncludedDirs?: (value: string) => void;
  includedFiles?: string;
  setIncludedFiles?: (value: string) => void;
}

export default function UserSelector({
  provider,
  setProvider,
  model,
  setModel,
  isCustomModel,
  setIsCustomModel,
  customModel,
  setCustomModel,

  // File filter configuration
  showFileFilters = false,
  excludedDirs = '',
  setExcludedDirs,
  excludedFiles = '',
  setExcludedFiles,
  includedDirs = '',
  setIncludedDirs,
  includedFiles = '',
  setIncludedFiles
}: ModelSelectorProps) {
  // State to manage the visibility of the filters modal and filter section
  const [isFilterSectionOpen, setIsFilterSectionOpen] = useState(false);
  // State to manage filter mode: 'exclude' or 'include'
  const [filterMode, setFilterMode] = useState<'exclude' | 'include'>('exclude');
  const { messages: t } = useLanguage();

  // State for model configurations from backend
  const [modelConfig, setModelConfig] = useState<ModelConfig | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // State for viewing default values
  const [showDefaultDirs, setShowDefaultDirs] = useState(false);
  const [showDefaultFiles, setShowDefaultFiles] = useState(false);

  // Fetch model configurations from the backend
  useEffect(() => {
    const fetchModelConfig = async () => {
      try {
        setIsLoading(true);
        setError(null);

        const response = await fetch('/api/models/config', { cache: 'no-store' });

        if (!response.ok) {
          throw new Error(`Error fetching model configurations: ${response.status}`);
        }

        const data = await response.json();
        setModelConfig(data);

        // Initialize defaults and discard stale cached models that are not
        // published by the currently configured Ollama endpoint.
        const activeProvider = provider || data.defaultProvider;
        if (!provider && activeProvider) {
          setProvider(activeProvider);
        }
        const selectedProvider = data.providers.find((p: Provider) => p.id === activeProvider);
        if (selectedProvider && selectedProvider.models.length > 0) {
          const modelStillAvailable = selectedProvider.models.some(
            (availableModel: Model) => availableModel.id === model
          );
          if (!modelStillAvailable) {
            setIsCustomModel(false);
            setCustomModel('');
            setModel(selectedProvider.models[0].id);
          }
        }
      } catch (err) {
        console.error('Failed to fetch model configurations:', err);
        setError('Failed to load model configurations. Using default options.');
      } finally {
        setIsLoading(false);
      }
    };

    fetchModelConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider, setModel, setProvider, setIsCustomModel, setCustomModel]); // Removed 'model' to prevent resetting custom model on every keystroke

  // API Key and Endpoint state
  const [apiKeys, setApiKeys] = useState<Record<string, string>>({});
  const [apiEndpoints, setApiEndpoints] = useState<Record<string, string>>({
    'openai_custom': 'https://api.openai.com/v1',
    'ollama': 'http://localhost:11434'
  });
  
  // Load saved keys from localStorage on mount
  useEffect(() => {
    try {
      const savedKeys = localStorage.getItem('deepwiki_api_keys');
      if (savedKeys) setApiKeys(JSON.parse(savedKeys));
      const savedEndpoints = localStorage.getItem('deepwiki_api_endpoints');
      if (savedEndpoints) setApiEndpoints(JSON.parse(savedEndpoints));
    } catch { console.error('Failed to parse saved api settings'); }
  }, []);

  const handleKeyChange = (prov: string, val: string) => {
    const newKeys = { ...apiKeys, [prov]: val };
    setApiKeys(newKeys);
    localStorage.setItem('deepwiki_api_keys', JSON.stringify(newKeys));
  };

  const handleEndpointChange = (prov: string, val: string) => {
    const newEndpoints = { ...apiEndpoints, [prov]: val };
    setApiEndpoints(newEndpoints);
    localStorage.setItem('deepwiki_api_endpoints', JSON.stringify(newEndpoints));
  };

  const [isProbing, setIsProbing] = useState(false);
  const [isSaved, setIsSaved] = useState(false);

  const handleSaveConfig = () => {
    setIsSaved(true);
    setTimeout(() => setIsSaved(false), 2000);
  };

  const handleReloadModels = async () => {
    setIsProbing(true);
    try {
      const endpoint = provider === 'ollama' 
        ? apiEndpoints['ollama'] || 'http://localhost:11434'
        : apiEndpoints[provider] || '';
        
      const response = await fetch('/api/models/probe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          endpoint: endpoint,
          api_key: apiKeys[provider] || '',
          provider_type: provider === 'ollama' ? 'ollama' : 'openai'
        })
      });
      if (response.ok) {
        const data = await response.json();
        if (data.models && data.models.length > 0 && modelConfig) {
            // Update the model list for the current provider
            const newConfig = { ...modelConfig };
            const p = newConfig.providers.find(p => p.id === provider);
            if (p) {
                p.models = data.models;
                setModelConfig(newConfig);
                setModel(data.models[0].id);
                setError(null);
            }
        } else {
            setError(data.error || 'No models found at the specified endpoint.');
        }
      }
    } catch (err) {
      console.error(err);
      setError('Failed to probe models from endpoint.');
    } finally {
      setIsProbing(false);
    }
  };

  // Handler for changing provider
  const handleProviderChange = (newProvider: string) => {
    setProvider(newProvider);
    setTimeout(() => {
      // Reset custom model state when changing providers
      setIsCustomModel(false);

      // Set default model for the selected provider
      if (modelConfig) {
        const selectedProvider = modelConfig.providers.find((p: Provider) => p.id === newProvider);
        if (selectedProvider && selectedProvider.models.length > 0) {
          setModel(selectedProvider.models[0].id);
        }
      }
    }, 10);
  };

  // Default excluded directories from config.py
  const defaultExcludedDirs =
`./.venv/
./venv/
./env/
./virtualenv/
./node_modules/
./bower_components/
./jspm_packages/
./.git/
./.svn/
./.hg/
./.bzr/
./__pycache__/
./.pytest_cache/
./.mypy_cache/
./.ruff_cache/
./.coverage/
./dist/
./build/
./out/
./target/
./bin/
./obj/
./docs/
./_docs/
./site-docs/
./_site/
./.idea/
./.vscode/
./.vs/
./.eclipse/
./.settings/
./logs/
./log/
./tmp/
./temp/
./.eng`;

  // Default excluded files from config.py
  const defaultExcludedFiles =
`package-lock.json
yarn.lock
pnpm-lock.yaml
npm-shrinkwrap.json
poetry.lock
Pipfile.lock
requirements.txt.lock
Cargo.lock
composer.lock
.lock
.DS_Store
Thumbs.db
desktop.ini
*.lnk
.env
.env.*
*.env
*.cfg
*.ini
.flaskenv
.gitignore
.gitattributes
.gitmodules
.github
.gitlab-ci.yml
.prettierrc
.eslintrc
.eslintignore
.stylelintrc
.editorconfig
.jshintrc
.pylintrc
.flake8
mypy.ini
pyproject.toml
tsconfig.json
webpack.config.js
babel.config.js
rollup.config.js
jest.config.js
karma.conf.js
vite.config.js
next.config.js
*.min.js
*.min.css
*.bundle.js
*.bundle.css
*.map
*.gz
*.zip
*.tar
*.tgz
*.rar
*.pyc
*.pyo
*.pyd
*.so
*.dll
*.class
*.exe
*.o
*.a
*.jpg
*.jpeg
*.png
*.gif
*.ico
*.svg
*.webp
*.mp3
*.mp4
*.wav
*.avi
*.mov
*.webm
*.csv
*.tsv
*.xls
*.xlsx
*.db
*.sqlite
*.sqlite3
*.pdf
*.docx
*.pptx`;

  // Display loading state
  if (isLoading) {
    return (
      <div className="flex flex-col gap-2">
        <div className="text-sm text-[var(--muted)]">Loading model configurations...</div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="space-y-4">
        {error && (
          <div className="text-sm text-red-500 mb-2">{error}</div>
        )}

        {/* Provider Selection */}
        <div>
          <label htmlFor="provider-dropdown" className="block text-xs font-medium text-[var(--foreground)] mb-1.5">
            {t.form?.modelProvider || 'Model Provider'}
          </label>
          <select
            id="provider-dropdown"
            value={provider}
            onChange={(e) => handleProviderChange(e.target.value)}
            className="input-japanese block w-full px-2.5 py-1.5 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
          >
            <option value="" disabled>{t.form?.selectProvider || 'Select Provider'}</option>
            {modelConfig?.providers.map((providerOption) => (
              <option key={providerOption.id} value={providerOption.id}>
                {providerOption.id === 'ollama'
                  ? 'Ollama'
                  : t.form?.[`provider${providerOption.id.charAt(0).toUpperCase() + providerOption.id.slice(1)}`] || providerOption.name}
              </option>
            ))}
          </select>
        </div>

        {/* Model Selection - consistent height regardless of type */}
        <div>
          <label htmlFor={isCustomModel ? "custom-model-input" : "model-dropdown"} className="block text-xs font-medium text-[var(--foreground)] mb-1.5">
            {t.form?.modelSelection || 'Model Selection'}
          </label>

          {isCustomModel ? (
            <div className="flex gap-2">
              <input
                id="custom-model-input"
                type="text"
                value={customModel}
                onChange={(e) => {
                  setCustomModel(e.target.value);
                  setModel(e.target.value);
                }}
                placeholder={t.form?.customModelPlaceholder || 'Enter custom model name'}
                className="input-japanese block w-full px-2.5 py-1.5 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
              />
            </div>
          ) : (
            <div className="flex gap-2">
              <select
                id="model-dropdown"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="input-japanese block w-full px-2.5 py-1.5 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                disabled={!provider || isLoading || !modelConfig?.providers.find(p => p.id === provider)?.models?.length}
              >
                {modelConfig?.providers.find((p: Provider) => p.id === provider)?.models.map((modelOption) => (
                  <option key={modelOption.id} value={modelOption.id}>
                    {modelOption.name}
                  </option>
                )) || <option value="">{t.form?.selectModel || 'Select Model'}</option>}
              </select>
              {(provider === 'ollama' || provider === 'openai_custom') && (
                <button
                  type="button"
                  onClick={handleReloadModels}
                  disabled={isProbing}
                  className="px-3 py-1.5 text-xs bg-[var(--accent-primary)]/10 text-[var(--accent-primary)] border border-[var(--accent-primary)]/30 rounded hover:bg-[var(--accent-primary)]/20 transition-colors whitespace-nowrap flex items-center justify-center min-w-[80px]"
                >
                  {isProbing ? '...' : 'Reload'}
                </button>
              )}
            </div>
          )}
        </div>

        {/* API Key and Endpoint Configuration */}
        {provider && provider !== 'local' && (
          <div className="p-3 bg-[var(--background)]/30 border border-[var(--border-color)]/50 rounded-md">
            <h4 className="text-xs font-semibold text-[var(--foreground)] mb-2 flex justify-between items-center">
              <span>{t.form?.connectionSettings || 'Connection Settings'}</span>
              {(provider === 'openai' || provider === 'claude' || provider === 'google') && (
                <span className="text-[10px] text-[var(--muted)] border border-[var(--border-color)] px-1.5 py-0.5 rounded cursor-help" title="Use your subscription login via CLI or get API Keys from the respective console">
                  Subscription Login Supported
                </span>
              )}
            </h4>
            
            {(provider === 'openai_custom' || provider === 'ollama') && (
              <div className="mb-3">
                <label className="block text-[10px] uppercase text-[var(--muted)] mb-1">API Endpoint URL</label>
                <input 
                  type="text" 
                  value={apiEndpoints[provider] || ''}
                  onChange={(e) => handleEndpointChange(provider, e.target.value)}
                  placeholder={provider === 'ollama' ? 'http://localhost:11434' : 'https://api.novita.ai/v3/openai'}
                  className="input-japanese block w-full px-2 py-1 text-xs rounded bg-black/20 text-[var(--foreground)] focus:border-[var(--accent-primary)] border-transparent"
                />
                {provider === 'openai_custom' && (
                  <div className="text-[9px] text-[var(--muted)] mt-1 leading-tight">
                    Examples: <code>https://api.novita.ai/v3/openai</code>, <code>https://api.together.xyz/v1</code>, <code>https://api.groq.com/openai/v1</code>, <code>http://localhost:8000/v1</code> (vLLM)
                  </div>
                )}
              </div>
            )}

            <div className="mb-2">
              <label className="block text-[10px] uppercase text-[var(--muted)] mb-1">
                API Key {provider === 'ollama' ? '(Optional)' : ''}
              </label>
              <div className="flex gap-2">
                <input 
                  type="password" 
                  value={apiKeys[provider] || ''}
                  onChange={(e) => handleKeyChange(provider, e.target.value)}
                  placeholder={`Enter ${provider} API Key...`}
                  className="input-japanese block w-full px-2 py-1 text-xs rounded bg-black/20 text-[var(--foreground)] focus:border-[var(--accent-primary)] border-transparent"
                />
                <button
                  type="button"
                  onClick={handleSaveConfig}
                  className={`px-3 py-1 text-xs rounded transition-colors whitespace-nowrap flex items-center justify-center min-w-[70px] ${
                    isSaved 
                      ? 'bg-green-500/20 text-green-400 border border-green-500/30' 
                      : 'bg-[var(--accent-primary)]/10 text-[var(--accent-primary)] border border-[var(--accent-primary)]/30 hover:bg-[var(--accent-primary)]/20'
                  }`}
                >
                  {isSaved ? '✓ Saved' : 'Save'}
                </button>
              </div>
            </div>
            
            {/* Subscription Helper Text */}
            {provider === 'openai' && (
              <div className="text-[10px] text-[var(--muted)] leading-tight mt-2 p-2 bg-[var(--accent-primary)]/5 rounded">
                <strong>ChatGPT Plus/Pro Subscription:</strong> Install the OpenAI Codex CLI <code>npm i -g @openai/codex</code>, run <code>codex login</code> in your terminal, and paste the generated access token above.
              </div>
            )}
            {provider === 'claude' && (
              <div className="text-[10px] text-[var(--muted)] leading-tight mt-2 p-2 bg-[var(--accent-primary)]/5 rounded">
                <strong>Claude Pro Subscription:</strong> Install Claude Code CLI <code>npm i -g @anthropic-ai/claude-code</code>, run <code>claude login</code>, and paste your session token above, OR use an API key from the Anthropic Console.
              </div>
            )}
            {provider === 'google' && (
              <div className="text-[10px] text-[var(--muted)] leading-tight mt-2 p-2 bg-[var(--accent-primary)]/5 rounded">
                <strong>Gemini Advanced Subscription:</strong> Install Google Antigravity CLI <code>npm i -g @google/agy</code>, run <code>agy auth login</code>, and paste your access token above, OR generate a free API key from Google AI Studio.
              </div>
            )}
            {provider === 'openai_custom' && (
              <div className="text-[10px] text-[var(--muted)] leading-tight mt-2 p-2 bg-[var(--accent-primary)]/5 rounded">
                <strong>Custom OpenAI-Compatible Provider:</strong> Enter the base URL of any OpenAI-compatible API (Novita, Together, Groq, vLLM, LM Studio, etc.). Use the <strong>Reload</strong> button to fetch available models, or enable <strong>Custom Model</strong> and type the model name manually. The API key from that provider goes in the field above.
              </div>
            )}
          </div>
        )}

        {/* Custom model toggle - only when provider supports it */}
        {modelConfig?.providers.find((p: Provider) => p.id === provider)?.supportsCustomModel && (
          <div className="mb-2">
            <div className="flex items-center pb-1">
              <div
                className="relative flex items-center cursor-pointer"
                onClick={() => {
                  const newValue = !isCustomModel;
                  setIsCustomModel(newValue);
                  if (newValue) {
                    setCustomModel(model);
                  }
                }}
              >
                <input
                  id="use-custom-model"
                  type="checkbox"
                  checked={isCustomModel}
                  onChange={() => {}}
                  className="sr-only"
                />
                <div className={`w-10 h-5 rounded-full transition-colors ${isCustomModel ? 'bg-[var(--accent-primary)]' : 'bg-gray-300 dark:bg-gray-600'}`}></div>
                <div className={`absolute left-0.5 top-0.5 w-4 h-4 rounded-full bg-white transition-transform transform ${isCustomModel ? 'translate-x-5' : ''}`}></div>
              </div>
              <label
                htmlFor="use-custom-model"
                className="ml-2 text-sm font-medium text-[var(--muted)] cursor-pointer"
                onClick={(e) => {
                  e.preventDefault();
                  const newValue = !isCustomModel;
                  setIsCustomModel(newValue);
                  if (newValue) {
                    setCustomModel(model);
                  }
                }}
              >
                {t.form?.useCustomModel || 'Use custom model'}
              </label>
            </div>
          </div>
        )}

        {showFileFilters && (
          <div className="mt-4">
            <button
              type="button"
              onClick={() => setIsFilterSectionOpen(!isFilterSectionOpen)}
              className="flex items-center text-sm text-[var(--accent-primary)] hover:text-[var(--accent-primary)]/80 transition-colors"
            >
              <span className="mr-1.5 text-xs">{isFilterSectionOpen ? '▼' : '►'}</span>
              {t.form?.advancedOptions || 'Advanced Options'}
            </button>

            {isFilterSectionOpen && (
              <div className="mt-3 p-3 border border-[var(--border-color)]/70 rounded-md bg-[var(--background)]/30">
                {/* Filter Mode Selection */}
                <div className="mb-4">
                  <label className="block text-sm font-medium text-[var(--foreground)] mb-2">
                    {t.form?.filterMode || 'Filter Mode'}
                  </label>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => setFilterMode('exclude')}
                      className={`flex-1 px-3 py-2 rounded-md border text-sm transition-colors ${
                        filterMode === 'exclude'
                          ? 'bg-[var(--accent-primary)]/10 border-[var(--accent-primary)] text-[var(--accent-primary)]'
                          : 'border-[var(--border-color)] text-[var(--foreground)] hover:bg-[var(--background)]'
                      }`}
                    >
                      {t.form?.excludeMode || 'Exclude Paths'}
                    </button>
                    <button
                      type="button"
                      onClick={() => setFilterMode('include')}
                      className={`flex-1 px-3 py-2 rounded-md border text-sm transition-colors ${
                        filterMode === 'include'
                          ? 'bg-[var(--accent-primary)]/10 border-[var(--accent-primary)] text-[var(--accent-primary)]'
                          : 'border-[var(--border-color)] text-[var(--foreground)] hover:bg-[var(--background)]'
                      }`}
                    >
                      {t.form?.includeMode || 'Include Only Paths'}
                    </button>
                  </div>
                  <p className="text-xs text-[var(--muted)] mt-1">
                    {filterMode === 'exclude'
                      ? (t.form?.excludeModeDescription || 'Specify paths to exclude from processing (default behavior)')
                      : (t.form?.includeModeDescription || 'Specify only the paths to include, ignoring all others')
                    }
                  </p>
                </div>

                {/* Directories Section */}
                <div className="mb-4">
                  <label className="block text-sm font-medium text-[var(--muted)] mb-1.5">
                    {filterMode === 'exclude'
                      ? (t.form?.excludedDirs || 'Excluded Directories')
                      : (t.form?.includedDirs || 'Included Directories')
                    }
                  </label>
                  <textarea
                    value={filterMode === 'exclude' ? excludedDirs : includedDirs}
                    onChange={(e) => {
                      if (filterMode === 'exclude') {
                        setExcludedDirs?.(e.target.value);
                      } else {
                        setIncludedDirs?.(e.target.value);
                      }
                    }}
                    rows={4}
                    className="block w-full rounded-md border border-[var(--border-color)]/50 bg-[var(--input-bg)] text-[var(--foreground)] px-3 py-2 text-sm focus:border-[var(--accent-primary)] focus:ring-1 focus:ring-opacity-50 shadow-sm"
                    placeholder={filterMode === 'exclude'
                      ? (t.form?.enterExcludedDirs || 'Enter excluded directories, one per line...')
                      : (t.form?.enterIncludedDirs || 'Enter included directories, one per line...')
                    }
                  />
                  {filterMode === 'exclude' && (
                    <>
                      <div className="flex mt-1.5">
                        <button
                          type="button"
                          onClick={() => setShowDefaultDirs(!showDefaultDirs)}
                          className="text-xs text-[var(--accent-primary)] hover:text-[var(--accent-primary)]/80 transition-colors"
                        >
                          {showDefaultDirs ? (t.form?.hideDefault || 'Hide Default') : (t.form?.viewDefault || 'View Default')}
                        </button>
                      </div>
                      {showDefaultDirs && (
                        <div className="mt-2 p-2 rounded bg-[var(--background)]/50 text-xs">
                          <p className="mb-1 text-[var(--muted)]">{t.form?.defaultNote || 'These defaults are already applied. Add your custom exclusions above.'}</p>
                          <pre className="whitespace-pre-wrap font-mono text-[var(--muted)] overflow-y-auto max-h-32">{defaultExcludedDirs}</pre>
                        </div>
                      )}
                    </>
                  )}
                </div>

                {/* Files Section */}
                <div>
                  <label className="block text-sm font-medium text-[var(--muted)] mb-1.5">
                    {filterMode === 'exclude'
                      ? (t.form?.excludedFiles || 'Excluded Files')
                      : (t.form?.includedFiles || 'Included Files')
                    }
                  </label>
                  <textarea
                    value={filterMode === 'exclude' ? excludedFiles : includedFiles}
                    onChange={(e) => {
                      if (filterMode === 'exclude') {
                        setExcludedFiles?.(e.target.value);
                      } else {
                        setIncludedFiles?.(e.target.value);
                      }
                    }}
                    rows={4}
                    className="block w-full rounded-md border border-[var(--border-color)]/50 bg-[var(--input-bg)] text-[var(--foreground)] px-3 py-2 text-sm focus:border-[var(--accent-primary)] focus:ring-1 focus:ring-opacity-50 shadow-sm"
                    placeholder={filterMode === 'exclude'
                      ? (t.form?.enterExcludedFiles || 'Enter excluded files, one per line...')
                      : (t.form?.enterIncludedFiles || 'Enter included files, one per line...')
                    }
                  />
                  {filterMode === 'exclude' && (
                    <>
                      <div className="flex mt-1.5">
                        <button
                          type="button"
                          onClick={() => setShowDefaultFiles(!showDefaultFiles)}
                          className="text-xs text-[var(--accent-primary)] hover:text-[var(--accent-primary)]/80 transition-colors"
                        >
                          {showDefaultFiles ? (t.form?.hideDefault || 'Hide Default') : (t.form?.viewDefault || 'View Default')}
                        </button>
                      </div>
                      {showDefaultFiles && (
                        <div className="mt-2 p-2 rounded bg-[var(--background)]/50 text-xs">
                          <p className="mb-1 text-[var(--muted)]">{t.form?.defaultNote || 'These defaults are already applied. Add your custom exclusions above.'}</p>
                          <pre className="whitespace-pre-wrap font-mono text-[var(--muted)] overflow-y-auto max-h-32">{defaultExcludedFiles}</pre>
                        </div>
                      )}
                    </>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
