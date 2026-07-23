'use client';

import React from 'react';
import { useLanguage } from '@/contexts/LanguageContext';
import { FaBookOpen, FaList, FaUserGraduate, FaCode } from 'react-icons/fa';
import {
  getDefaultWikiPageCount,
  MAX_WIKI_PAGE_COUNT,
  MIN_WIKI_PAGE_COUNT,
  normalizeWikiPageCount,
} from '@/utils/wikiPageCount';

interface WikiTypeSelectorProps {
  isComprehensiveView: boolean;
  setIsComprehensiveView: (value: boolean) => void;
  pageCount?: number;
  setPageCount?: (value: number) => void;
  // Audience mode is orthogonal to comprehensive/concise (depth) -- optional
  // so callers that don't offer it (e.g. website wikis, which already have
  // their own technical/content-mode toggle) can simply omit the props.
  isUserFocusedView?: boolean;
  setIsUserFocusedView?: (value: boolean) => void;
}

const WikiTypeSelector: React.FC<WikiTypeSelectorProps> = ({
  isComprehensiveView,
  setIsComprehensiveView,
  pageCount,
  setPageCount,
  isUserFocusedView,
  setIsUserFocusedView,
}) => {
  const { messages: t } = useLanguage();
  const selectWikiType = (isComprehensive: boolean) => {
    setIsComprehensiveView(isComprehensive);
    setPageCount?.(getDefaultWikiPageCount(isComprehensive));
  };

  return (
    <div className="mb-4">
      <label className="block text-sm font-medium text-[var(--foreground)] mb-2">
        {t.form?.wikiType || 'Wiki Type'}
      </label>
      <div className="flex flex-col sm:flex-row gap-3">
        <button
          type="button"
          onClick={() => selectWikiType(true)}
          className={`flex items-center justify-between p-3 rounded-md border transition-colors ${
            isComprehensiveView
              ? 'bg-[var(--accent-primary)]/10 border-[var(--accent-primary)]/30 text-[var(--accent-primary)]'
              : 'bg-[var(--background)]/50 border-[var(--border-color)] text-[var(--foreground)] hover:bg-[var(--background)]'
          }`}
        >
          <div className="flex items-center">
            <FaBookOpen className="mr-2" />
            <div className="text-left">
              <div className="font-medium">{t.form?.comprehensive || 'Comprehensive'}</div>
              <div className="text-xs opacity-80">
                {t.form?.comprehensiveDescription || 'Detailed wiki with structured sections and more pages'}
              </div>
            </div>
          </div>
          {isComprehensiveView && (
            <div className="ml-2 h-4 w-4 rounded-full bg-[var(--accent-primary)]/20 flex items-center justify-center">
              <div className="h-2 w-2 rounded-full bg-[var(--accent-primary)]"></div>
            </div>
          )}
        </button>
        
        <button
          type="button"
          onClick={() => selectWikiType(false)}
          className={`flex items-center justify-between p-3 rounded-md border transition-colors ${
            !isComprehensiveView
              ? 'bg-[var(--accent-primary)]/10 border-[var(--accent-primary)]/30 text-[var(--accent-primary)]'
              : 'bg-[var(--background)]/50 border-[var(--border-color)] text-[var(--foreground)] hover:bg-[var(--background)]'
          }`}
        >
          <div className="flex items-center">
            <FaList className="mr-2" />
            <div className="text-left">
              <div className="font-medium">{t.form?.concise || 'Concise'}</div>
              <div className="text-xs opacity-80">
                {t.form?.conciseDescription || 'Simplified wiki with fewer pages and essential information'}
              </div>
            </div>
          </div>
          {!isComprehensiveView && (
            <div className="ml-2 h-4 w-4 rounded-full bg-[var(--accent-primary)]/20 flex items-center justify-center">
              <div className="h-2 w-2 rounded-full bg-[var(--accent-primary)]"></div>
            </div>
          )}
        </button>
      </div>
      {pageCount !== undefined && setPageCount && (
        <div className="mt-3">
          <label
            htmlFor="wiki-page-count"
            className="block text-sm font-medium text-[var(--foreground)] mb-1"
          >
            {t.form?.pageCount || 'Number of pages'}
          </label>
          <input
            id="wiki-page-count"
            type="number"
            min={MIN_WIKI_PAGE_COUNT}
            max={MAX_WIKI_PAGE_COUNT}
            value={pageCount}
            onChange={(event) =>
              setPageCount(
                normalizeWikiPageCount(
                  event.target.value,
                  isComprehensiveView,
                ),
              )
            }
            className="input-japanese block w-full px-3 py-2 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
          />
          <p className="mt-1 text-xs text-[var(--muted)]">
            {(t.form?.pageCountHelp || 'Choose between {min} and {max} pages.')
              .replace('{min}', String(MIN_WIKI_PAGE_COUNT))
              .replace('{max}', String(MAX_WIKI_PAGE_COUNT))}
          </p>
        </div>
      )}
      {isUserFocusedView !== undefined && setIsUserFocusedView && (
        <div className="mt-4">
          <label className="block text-sm font-medium text-[var(--foreground)] mb-2">
            {t.form?.wikiAudience || 'Wiki Audience'}
          </label>
          <div className="flex flex-col sm:flex-row gap-3">
            <button
              type="button"
              onClick={() => setIsUserFocusedView(false)}
              className={`flex items-center justify-between p-3 rounded-md border transition-colors ${
                !isUserFocusedView
                  ? 'bg-[var(--accent-primary)]/10 border-[var(--accent-primary)]/30 text-[var(--accent-primary)]'
                  : 'bg-[var(--background)]/50 border-[var(--border-color)] text-[var(--foreground)] hover:bg-[var(--background)]'
              }`}
            >
              <div className="flex items-center">
                <FaCode className="mr-2" />
                <div className="text-left">
                  <div className="font-medium">{t.form?.audienceDeveloper || 'For Developers'}</div>
                  <div className="text-xs opacity-80">
                    {t.form?.audienceDeveloperDescription || 'Architecture, implementation, and code-level detail'}
                  </div>
                </div>
              </div>
              {!isUserFocusedView && (
                <div className="ml-2 h-4 w-4 rounded-full bg-[var(--accent-primary)]/20 flex items-center justify-center">
                  <div className="h-2 w-2 rounded-full bg-[var(--accent-primary)]"></div>
                </div>
              )}
            </button>

            <button
              type="button"
              onClick={() => setIsUserFocusedView(true)}
              className={`flex items-center justify-between p-3 rounded-md border transition-colors ${
                isUserFocusedView
                  ? 'bg-[var(--accent-primary)]/10 border-[var(--accent-primary)]/30 text-[var(--accent-primary)]'
                  : 'bg-[var(--background)]/50 border-[var(--border-color)] text-[var(--foreground)] hover:bg-[var(--background)]'
              }`}
            >
              <div className="flex items-center">
                <FaUserGraduate className="mr-2" />
                <div className="text-left">
                  <div className="font-medium">{t.form?.audienceUser || 'For End Users'}</div>
                  <div className="text-xs opacity-80">
                    {t.form?.audienceUserDescription || 'Installation, configuration, features, and usage -- no source code'}
                  </div>
                </div>
              </div>
              {isUserFocusedView && (
                <div className="ml-2 h-4 w-4 rounded-full bg-[var(--accent-primary)]/20 flex items-center justify-center">
                  <div className="h-2 w-2 rounded-full bg-[var(--accent-primary)]"></div>
                </div>
              )}
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default WikiTypeSelector;
