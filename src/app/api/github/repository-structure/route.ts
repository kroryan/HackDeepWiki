import { execFile } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';
import { NextRequest } from 'next/server';

export const dynamic = 'force-dynamic';

const execFileAsync = promisify(execFile);
const SAFE_REPO_PART = /^[A-Za-z0-9_.-]+$/;
const MAX_BUFFER = 32 * 1024 * 1024;

async function git(args: string[], cwd?: string) {
  return execFileAsync('git', args, {
    cwd,
    timeout: 120_000,
    maxBuffer: MAX_BUFFER,
    env: {
      ...process.env,
      GIT_LFS_SKIP_SMUDGE: '1',
      GIT_TERMINAL_PROMPT: '0',
    },
  });
}

export async function GET(request: NextRequest) {
  const owner = request.nextUrl.searchParams.get('owner')?.trim() || '';
  const repo = request.nextUrl.searchParams.get('repo')?.trim() || '';

  if (!SAFE_REPO_PART.test(owner) || !SAFE_REPO_PART.test(repo)) {
    return Response.json(
      { message: 'Invalid GitHub owner or repository name.' },
      { status: 400 }
    );
  }

  const checkout = await mkdtemp(join(tmpdir(), 'hackdeepwiki-github-'));
  const repositoryUrl = `https://github.com/${owner}/${repo}.git`;

  try {
    await git(
      [
        'clone',
        '--depth',
        '1',
        '--filter=blob:none',
        '--no-tags',
        '--no-checkout',
        '--single-branch',
        repositoryUrl,
        checkout,
      ]
    );

    const [{ stdout: branchOutput }, { stdout: filesOutput }] =
      await Promise.all([
        git(['branch', '--show-current'], checkout),
        git(['ls-tree', '-r', '--name-only', 'HEAD'], checkout),
      ]);

    const paths = filesOutput
      .split('\n')
      .map((path) => path.trim())
      .filter(Boolean);
    const readmePath = paths.find(
      (path) => !path.includes('/') && /^readme(?:\.[^.]+)?$/i.test(path)
    );

    let readme = '';
    if (readmePath) {
      const { stdout } = await git(['show', `HEAD:${readmePath}`], checkout);
      readme = stdout;
    }

    return Response.json(
      {
        default_branch: branchOutput.trim() || 'main',
        tree: paths.map((path) => ({ path, type: 'blob' })),
        readme,
        source: 'git-fallback',
      },
      {
        headers: {
          'Cache-Control': 'no-store, max-age=0',
        },
      }
    );
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Unknown Git error';
    return Response.json(
      {
        message: `Could not read public repository through Git: ${message}`,
      },
      { status: 502 }
    );
  } finally {
    await rm(checkout, { recursive: true, force: true });
  }
}
