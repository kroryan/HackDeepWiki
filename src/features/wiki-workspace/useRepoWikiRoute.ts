'use client';

import { RepoInfo } from '@/types/repoinfo';
import { normalizeWikiPageCount } from '@/utils/wikiPageCount';
import { useParams, useSearchParams } from 'next/navigation';
import { useMemo } from 'react';

function decoded(value: string | null): string {
  if (!value) return '';
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function useRepoWikiRoute() {
  const params = useParams();
  const searchParams = useSearchParams();
  const owner = params.owner as string;
  const repo = params.repo as string;
  const token = searchParams.get('token') || '';
  const localPath = decoded(searchParams.get('local_path')) || undefined;
  const repoUrl = decoded(searchParams.get('repo_url')) || undefined;
  const providerParam = searchParams.get('provider') || '';
  const modelParam = searchParams.get('model') || '';
  const isCustomModelParam = searchParams.get('is_custom_model') === 'true';
  const customModelParam = searchParams.get('custom_model') || '';
  const language = searchParams.get('language') || 'en';
  const isComprehensiveParam = searchParams.get('comprehensive') !== 'false';
  const pageCountParam = normalizeWikiPageCount(
    searchParams.get('pages'),
    isComprehensiveParam,
  );
  const isUserFocusedParam = searchParams.get('audience') === 'user';
  const focusInstructionsParam = decoded(searchParams.get('focus'));

  let repoHost = '';
  if (repoUrl) {
    try {
      repoHost = new URL(repoUrl).hostname.toLowerCase();
    } catch {
      // The backend reports a useful validation error when the URL is used.
    }
  }
  const repoType = repoHost.includes('bitbucket')
    ? 'bitbucket'
    : repoHost.includes('gitlab')
      ? 'gitlab'
      : repoHost.includes('github')
        ? 'github'
        : searchParams.get('type') || 'github';

  const repoInfo = useMemo<RepoInfo>(() => ({
    owner,
    repo,
    type: repoType,
    token: token || null,
    localPath: localPath || null,
    repoUrl: repoUrl || null,
  }), [localPath, owner, repo, repoType, repoUrl, token]);

  return {
    searchParams,
    owner,
    repo,
    token,
    localPath,
    repoUrl,
    providerParam,
    modelParam,
    isCustomModelParam,
    customModelParam,
    language,
    isComprehensiveParam,
    pageCountParam,
    isUserFocusedParam,
    focusInstructionsParam,
    repoType,
    repoInfo,
    vulnScanRequested: searchParams.get('vuln_scan') === '1',
    vulnClientEnabled: searchParams.get('vuln_client') !== '0',
    vulnServerEnabled: searchParams.get('vuln_server') !== '0',
    vulnDepsEnabled: searchParams.get('vuln_deps') !== '0',
    nvdKeyParam: decoded(searchParams.get('nvd_key')),
    crawlScopeModeParam: (
      searchParams.get('crawl_scope_mode') as 'count' | 'subdomains' | 'all'
    ) || 'count',
    crawlMaxPagesParam: Number(searchParams.get('crawl_max_pages')) || 60,
    crawlSubdomainsParam: decoded(searchParams.get('crawl_subdomains')),
    crawlRespectRobotsParam: searchParams.get('crawl_respect_robots') !== '0',
    technicalAnalysisEnabled: searchParams.get('technical_analysis') === '1',
    deepScanEnabled: searchParams.get('deep_scan') === '1',
  };
}
