'use client';

import type { VulnReport } from '@/components/vuln/types';
import type { WebVulnReport } from '@/components/vuln/webTypes';
import type RepoInfo from '@/types/repoinfo';
import { getSavedApiCredentials } from '@/utils/apiCredentials';
import { getBackendWebSocketUrl } from '@/utils/backendUrl';
import getRepoUrl from '@/utils/getRepoUrl';
import type {
  ScanRelease,
  VulnScanOverrides,
  WebVulnScanOverrides,
} from '@/utils/repoWikiHelpers';
import { useCallback, useEffect, useRef } from 'react';

import { useSecurityScanWorkspace } from './useSecurityScanWorkspace';

interface ScanMessage<T> {
  type: 'progress' | 'done' | 'error';
  message?: string;
  percent?: number;
  report?: T;
  version?: number;
}

interface StreamScanOptions<T> {
  path: string;
  payload: object;
  timeoutMs: number;
  timeoutMessage: string;
  onProgress: (message: string, percent: number | null) => void;
  onDone: (report: T, version?: number) => void;
}

async function streamScan<T>({
  path,
  payload,
  timeoutMs,
  timeoutMessage,
  onProgress,
  onDone,
}: StreamScanOptions<T>): Promise<void> {
  const socketUrl = await getBackendWebSocketUrl(path);
  await new Promise<void>((resolve, reject) => {
    const socket = new WebSocket(socketUrl);
    let settled = false;
    let hadTransportError = false;
    const timeout = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      socket.close();
      reject(new Error(timeoutMessage));
    }, timeoutMs);

    const finish = (action: () => void) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeout);
      action();
    };

    socket.onopen = () => socket.send(JSON.stringify(payload));
    socket.onmessage = event => {
      let message: ScanMessage<T>;
      try {
        message = JSON.parse(event.data) as ScanMessage<T>;
      } catch {
        return;
      }
      if (message.type === 'progress') {
        onProgress(
          message.message || 'Working…',
          typeof message.percent === 'number' ? message.percent : null,
        );
      } else if (message.type === 'done' && message.report) {
        finish(() => {
          onDone(message.report as T, message.version);
          socket.close();
          resolve();
        });
      } else if (message.type === 'error') {
        finish(() => {
          socket.close();
          reject(new Error(message.message || 'Scan failed.'));
        });
      }
    };
    socket.onerror = () => {
      hadTransportError = true;
    };
    socket.onclose = event => {
      if (settled) return;
      finish(() => {
        const abnormal =
          hadTransportError || (event.code !== 1000 && event.code !== 1005);
        if (abnormal) {
          const detail = event.reason
            ? `: ${event.reason}`
            : ` (code ${event.code})`;
          reject(new Error(`WebSocket error during scan${detail}.`));
        } else {
          resolve();
        }
      });
    };
  });
}

interface Options {
  effectiveRepoInfo: RepoInfo;
  repoUrl: string | undefined;
  repoType: string;
  language: string;
  selectedProvider: string;
  selectedModel: string;
  currentToken: string;
  nvdKey: string;
  vulnClientEnabled: boolean;
  vulnServerEnabled: boolean;
  vulnDepsEnabled: boolean;
  deepScanEnabled: boolean;
  excludedDirs: string;
  excludedFiles: string;
}

export function useRepositorySecurityScans({
  effectiveRepoInfo,
  repoUrl,
  repoType,
  language,
  selectedProvider,
  selectedModel,
  currentToken,
  nvdKey,
  vulnClientEnabled,
  vulnServerEnabled,
  vulnDepsEnabled,
  deepScanEnabled,
  excludedDirs,
  excludedFiles,
}: Options) {
  const dependency = useSecurityScanWorkspace<VulnReport>();
  const website = useSecurityScanWorkspace<WebVulnReport>();
  const dependencyActions = useRef(dependency);
  const websiteActions = useRef(website);
  const dependencyRunning = useRef(false);
  const websiteRunning = useRef(false);
  useEffect(() => {
    dependencyActions.current = dependency;
    websiteActions.current = website;
  }, [dependency, website]);

  const dependencyParams = useCallback((version?: number) => {
    const values: Record<string, string> = {
      owner: effectiveRepoInfo.owner,
      repo: effectiveRepoInfo.repo,
      repo_type: repoType,
      language,
    };
    if (version != null) values.version = String(version);
    return new URLSearchParams(values);
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, repoType, language]);

  const websiteParams = useCallback((version?: number) => {
    const values: Record<string, string> = {
      owner: effectiveRepoInfo.owner,
      repo: effectiveRepoInfo.repo,
      language,
    };
    if (version != null) values.version = String(version);
    return new URLSearchParams(values);
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, language]);

  const loadVulnReleases = useCallback(async (autoSelectVersion?: number) => {
    try {
      const response = await fetch(
        `/api/vuln_cache/releases?${dependencyParams()}`,
      );
      if (!response.ok) return;
      const data = await response.json();
      const releases: ScanRelease[] = Array.isArray(data?.releases)
        ? data.releases
        : [];
      dependencyActions.current.setReleases(releases);
      if (autoSelectVersion != null) {
        dependencyActions.current.setSelectedVersion(autoSelectVersion);
      } else if (releases.length > 0) {
        dependencyActions.current.setSelectedVersion(previous =>
          previous == null ? releases[0].version : previous
        );
      }
    } catch (error) {
      console.warn('Error loading vulnerability releases:', error);
    }
  }, [dependencyParams]);

  const loadVulnRelease = useCallback(async (version: number) => {
    if (!version) return;
    try {
      const response = await fetch(`/api/vuln_cache?${dependencyParams(version)}`);
      if (!response.ok) {
        throw new Error(`Failed to load release v${version}: ${response.status}`);
      }
      dependencyActions.current.setReport(await response.json() as VulnReport);
      dependencyActions.current.setStatus('done');
      dependencyActions.current.setSelectedVersion(version);
    } catch (error) {
      console.warn('Error loading vulnerability release:', error);
    }
  }, [dependencyParams]);

  const deleteVulnRelease = useCallback(async (version: number) => {
    if (
      !version ||
      !window.confirm(
        `Delete security scan release v${version}? This cannot be undone.`,
      )
    ) return;
    try {
      const response = await fetch(`/api/vuln_cache?${dependencyParams(version)}`, {
        method: 'DELETE',
      });
      if (!response.ok) {
        throw new Error(`Failed to delete release v${version}: ${response.status}`);
      }
      const releasesResponse = await fetch(
        `/api/vuln_cache/releases?${dependencyParams()}`,
      );
      const data = releasesResponse.ok
        ? await releasesResponse.json()
        : { releases: [] };
      const remaining: ScanRelease[] = Array.isArray(data?.releases)
        ? data.releases
        : [];
      dependencyActions.current.setReleases(remaining);
      if (remaining.length > 0) {
        await loadVulnRelease(remaining[0].version);
      } else {
        dependencyActions.current.setSelectedVersion(null);
        dependencyActions.current.setReport(null);
        dependencyActions.current.setStatus('idle');
      }
    } catch (error) {
      console.warn('Error deleting vulnerability release:', error);
    }
  }, [dependencyParams, loadVulnRelease]);

  const runVulnScan = useCallback(async (overrides?: VulnScanOverrides) => {
    if (dependencyRunning.current) return;
    dependencyRunning.current = true;
    dependencyActions.current.setStatus('running');
    dependencyActions.current.setError(null);
    dependencyActions.current.setReport(null);
    dependencyActions.current.setProgressMessage('Starting scan…');
    dependencyActions.current.setProgressPercent(0);
    const provider = overrides?.provider ?? selectedProvider;
    const model = overrides?.model ?? selectedModel;
    const credentials = getSavedApiCredentials(provider);
    try {
      await streamScan<VulnReport>({
        path: '/ws/vuln_scan',
        timeoutMs: 10 * 60 * 1000,
        timeoutMessage: 'Vulnerability scan timed out.',
        payload: {
          repo_url: repoUrl || getRepoUrl(effectiveRepoInfo),
          repo_type: repoType,
          owner: effectiveRepoInfo.owner,
          repo: effectiveRepoInfo.repo,
          language,
          provider,
          model,
          api_key: credentials.api_key || undefined,
          api_endpoint: credentials.api_endpoint || undefined,
          local_path: effectiveRepoInfo.localPath || undefined,
          token: currentToken || undefined,
          force: overrides !== undefined,
          nvd_key: (overrides?.nvdKey ?? nvdKey) || undefined,
          enable_client: overrides?.vulnClient ?? vulnClientEnabled,
          enable_server: overrides?.vulnServer ?? vulnServerEnabled,
          enable_deps: overrides?.vulnDeps ?? vulnDepsEnabled,
          run_llm: true,
          excluded_dirs: excludedDirs,
          excluded_files: excludedFiles,
        },
        onProgress: (message, percent) => {
          dependencyActions.current.setProgressMessage(message);
          dependencyActions.current.setProgressPercent(percent);
        },
        onDone: (report, version) => {
          dependencyActions.current.setReport(report);
          dependencyActions.current.setStatus('done');
          dependencyActions.current.setProgressPercent(100);
          void loadVulnReleases(version);
        },
      });
    } catch (error) {
      dependencyActions.current.setStatus('error');
      dependencyActions.current.setError(
        error instanceof Error ? error.message : 'Scan failed.',
      );
    } finally {
      dependencyRunning.current = false;
    }
  }, [
    currentToken,
    effectiveRepoInfo,
    excludedDirs,
    excludedFiles,
    language,
    loadVulnReleases,
    nvdKey,
    repoType,
    repoUrl,
    selectedModel,
    selectedProvider,
    vulnClientEnabled,
    vulnDepsEnabled,
    vulnServerEnabled,
  ]);

  const loadVulnCache = useCallback(async () => {
    try {
      const response = await fetch(`/api/vuln_cache?${dependencyParams()}`);
      if (response.ok) {
        dependencyActions.current.setReport(await response.json() as VulnReport);
        dependencyActions.current.setStatus('done');
      }
    } catch {
      // No persisted scan yet.
    }
  }, [dependencyParams]);

  const loadWebVulnReleases = useCallback(async (autoSelectVersion?: number) => {
    try {
      const response = await fetch(
        `/api/web_vuln_cache/releases?${websiteParams()}`,
      );
      if (!response.ok) return;
      const data = await response.json();
      const releases: ScanRelease[] = Array.isArray(data?.releases)
        ? data.releases
        : [];
      websiteActions.current.setReleases(releases);
      if (autoSelectVersion != null) {
        websiteActions.current.setSelectedVersion(autoSelectVersion);
      } else if (releases.length > 0) {
        websiteActions.current.setSelectedVersion(previous =>
          previous == null ? releases[0].version : previous
        );
      }
    } catch (error) {
      console.warn('Error loading website vulnerability releases:', error);
    }
  }, [websiteParams]);

  const loadWebVulnRelease = useCallback(async (version: number) => {
    if (!version) return;
    try {
      const response = await fetch(
        `/api/web_vuln_cache?${websiteParams(version)}`,
      );
      if (!response.ok) {
        throw new Error(`Failed to load release v${version}: ${response.status}`);
      }
      websiteActions.current.setReport(await response.json() as WebVulnReport);
      websiteActions.current.setStatus('done');
      websiteActions.current.setSelectedVersion(version);
    } catch (error) {
      console.warn('Error loading website vulnerability release:', error);
    }
  }, [websiteParams]);

  const deleteWebVulnRelease = useCallback(async (version: number) => {
    if (
      !version ||
      !window.confirm(
        `Delete website security scan release v${version}? This cannot be undone.`,
      )
    ) return;
    try {
      const response = await fetch(
        `/api/web_vuln_cache?${websiteParams(version)}`,
        { method: 'DELETE' },
      );
      if (!response.ok) {
        throw new Error(`Failed to delete release v${version}: ${response.status}`);
      }
      const releasesResponse = await fetch(
        `/api/web_vuln_cache/releases?${websiteParams()}`,
      );
      const data = releasesResponse.ok
        ? await releasesResponse.json()
        : { releases: [] };
      const remaining: ScanRelease[] = Array.isArray(data?.releases)
        ? data.releases
        : [];
      websiteActions.current.setReleases(remaining);
      if (remaining.length > 0) {
        await loadWebVulnRelease(remaining[0].version);
      } else {
        websiteActions.current.setSelectedVersion(null);
        websiteActions.current.setReport(null);
        websiteActions.current.setStatus('idle');
      }
    } catch (error) {
      console.warn('Error deleting website vulnerability release:', error);
    }
  }, [loadWebVulnRelease, websiteParams]);

  const runWebVulnScan = useCallback(async (
    overrides?: WebVulnScanOverrides,
  ) => {
    if (websiteRunning.current) return;
    websiteRunning.current = true;
    websiteActions.current.setStatus('running');
    websiteActions.current.setError(null);
    websiteActions.current.setReport(null);
    websiteActions.current.setProgressMessage('Starting scan…');
    websiteActions.current.setProgressPercent(0);
    const provider = overrides?.provider ?? selectedProvider;
    const model = overrides?.model ?? selectedModel;
    const credentials = getSavedApiCredentials(provider);
    try {
      await streamScan<WebVulnReport>({
        path: '/ws/web_vuln_scan',
        timeoutMs: 20 * 60 * 1000,
        timeoutMessage: 'Website vulnerability scan timed out.',
        payload: {
          site_url: repoUrl || getRepoUrl(effectiveRepoInfo),
          owner: effectiveRepoInfo.owner,
          repo: effectiveRepoInfo.repo,
          language,
          provider,
          model,
          api_key: credentials.api_key || undefined,
          api_endpoint: credentials.api_endpoint || undefined,
          run_llm: true,
          enable_deep_scan: overrides?.enableDeepScan ?? deepScanEnabled,
        },
        onProgress: (message, percent) => {
          websiteActions.current.setProgressMessage(message);
          websiteActions.current.setProgressPercent(percent);
        },
        onDone: (report, version) => {
          websiteActions.current.setReport(report);
          websiteActions.current.setStatus('done');
          websiteActions.current.setProgressPercent(100);
          void loadWebVulnReleases(version);
        },
      });
    } catch (error) {
      websiteActions.current.setStatus('error');
      websiteActions.current.setError(
        error instanceof Error ? error.message : 'Scan failed.',
      );
    } finally {
      websiteRunning.current = false;
    }
  }, [
    deepScanEnabled,
    effectiveRepoInfo,
    language,
    loadWebVulnReleases,
    repoUrl,
    selectedModel,
    selectedProvider,
  ]);

  const loadWebVulnCache = useCallback(async () => {
    try {
      const response = await fetch(`/api/web_vuln_cache?${websiteParams()}`);
      if (response.ok) {
        websiteActions.current.setReport(await response.json() as WebVulnReport);
        websiteActions.current.setStatus('done');
      }
    } catch {
      // No persisted scan yet.
    }
  }, [websiteParams]);

  useEffect(() => {
    void loadVulnCache();
  }, [loadVulnCache]);
  useEffect(() => {
    if (
      effectiveRepoInfo.type !== 'website' &&
      effectiveRepoInfo.type !== 'fanwiki'
    ) {
      void loadVulnReleases();
    }
  }, [effectiveRepoInfo.type, loadVulnReleases]);
  useEffect(() => {
    if (effectiveRepoInfo.type === 'website') {
      void loadWebVulnCache();
      void loadWebVulnReleases();
    }
  }, [
    effectiveRepoInfo.type,
    loadWebVulnCache,
    loadWebVulnReleases,
  ]);

  const runVulnScanRef = useRef(runVulnScan);
  const runWebVulnScanRef = useRef(runWebVulnScan);
  useEffect(() => {
    runVulnScanRef.current = runVulnScan;
  }, [runVulnScan]);
  useEffect(() => {
    runWebVulnScanRef.current = runWebVulnScan;
  }, [runWebVulnScan]);

  return {
    dependency,
    website,
    runVulnScan,
    runWebVulnScan,
    runVulnScanRef,
    runWebVulnScanRef,
    loadVulnRelease,
    deleteVulnRelease,
    loadWebVulnRelease,
    deleteWebVulnRelease,
  };
}
