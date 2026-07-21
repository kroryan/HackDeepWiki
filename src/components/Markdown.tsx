import React, { useState } from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeRaw from 'rehype-raw';
import rehypeKatex from 'rehype-katex';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { tomorrow } from 'react-syntax-highlighter/dist/cjs/styles/prism';
import Mermaid from './Mermaid';
import CodeViewer from './CodeViewer';
import RepoInfo from '@/types/repoinfo';
import 'katex/dist/katex.min.css';

interface MarkdownProps {
  content: string;
  // Needed only to open the in-app code viewer for source-file citations
  // (see the `a` component below) -- callers rendering content that never
  // has these, if any, can omit it.
  repoInfo?: RepoInfo;
}

// Matches a "Sources: [README.md:1-30]()" style citation's link TEXT (the
// href is deliberately left empty by the wiki-generation prompt -- see
// src/app/[owner]/[repo]/page.tsx's generation prompt for the exact
// "Sources: [filename.ext:start-end]()" format the model is instructed to
// use). Extracts just the file path, dropping an optional ":line" or
// ":start-end" suffix. Requires an extension so plain link text like
// "here" or a version string doesn't get misread as a file citation.
const FILE_CITATION_RE = /^([\w./-]+\.\w+)(?::\d+(?:-\d+)?)?$/;

function parseFileCitation(children: React.ReactNode): string | null {
  const text =
    typeof children === 'string'
      ? children
      : Array.isArray(children) && children.length === 1 && typeof children[0] === 'string'
        ? children[0]
        : null;
  if (!text) return null;
  const match = FILE_CITATION_RE.exec(text.trim());
  return match ? match[1] : null;
}

const Markdown: React.FC<MarkdownProps> = ({ content, repoInfo }) => {
  const [openCodeFile, setOpenCodeFile] = useState<string | null>(null);
  // Define markdown components
  const MarkdownComponents: React.ComponentProps<typeof ReactMarkdown>['components'] = {
    p({ children, ...props }: { children?: React.ReactNode }) {
      return <p className="mb-3 text-sm leading-relaxed text-[var(--foreground)]" {...props}>{children}</p>;
    },
    h1({ children, ...props }: { children?: React.ReactNode }) {
      return <h1 className="text-xl font-bold mt-6 mb-3 text-[var(--foreground)] font-mono" {...props}>{children}</h1>;
    },
    h2({ children, ...props }: { children?: React.ReactNode }) {
      // Special styling for ReAct headings
      if (children && typeof children === 'string') {
        const text = children.toString();
        if (text.includes('Thought') || text.includes('Action') || text.includes('Observation') || text.includes('Answer')) {
          const tone =
            text.includes('Thought') ? 'var(--accent-secondary)' :
            text.includes('Action') ? 'var(--highlight)' :
            text.includes('Observation') ? '#f5a623' :
            'var(--accent-primary)';
          return (
            <h2
              className="text-base font-bold mt-5 mb-3 p-2 rounded border-l-2"
              style={{
                color: tone,
                borderColor: tone,
                background: `color-mix(in srgb, ${tone} 10%, transparent)`,
              }}
              {...props}
            >
              {children}
            </h2>
          );
        }
      }
      return <h2 className="text-lg font-bold mt-5 mb-3 text-[var(--foreground)] font-mono" {...props}>{children}</h2>;
    },
    h3({ children, ...props }: { children?: React.ReactNode }) {
      return <h3 className="text-base font-semibold mt-4 mb-2 text-[var(--foreground)] font-mono" {...props}>{children}</h3>;
    },
    h4({ children, ...props }: { children?: React.ReactNode }) {
      return <h4 className="text-sm font-semibold mt-3 mb-2 text-[var(--foreground)] font-mono" {...props}>{children}</h4>;
    },
    ul({ children, ...props }: { children?: React.ReactNode }) {
      return <ul className="list-disc pl-6 mb-4 text-sm text-[var(--foreground)] space-y-2" {...props}>{children}</ul>;
    },
    ol({ children, ...props }: { children?: React.ReactNode }) {
      return <ol className="list-decimal pl-6 mb-4 text-sm text-[var(--foreground)] space-y-2" {...props}>{children}</ol>;
    },
    li({ children, ...props }: { children?: React.ReactNode }) {
      return <li className="mb-2 text-sm leading-relaxed text-[var(--foreground)]" {...props}>{children}</li>;
    },
    a({ children, href, ...props }: { children?: React.ReactNode; href?: string }) {
      // Two forms of "this is a repo source file, not a real URL" citation:
      //  - `codefile:<path>` (api/search_tool.py's format_sources_footer,
      //    a chat answer's "pages consulted" footer).
      //  - An empty-href markdown link whose TEXT is the citation itself,
      //    e.g. `[README.md:1-30]()` -- the format the wiki-generation
      //    prompt instructs the model to use for "Sources: ..." lines in
      //    generated wiki pages (see src/app/[owner]/[repo]/page.tsx's
      //    generation prompt). Both open the in-app CodeViewer instead of
      //    letting the browser try to navigate to a fake/empty URL.
      const citedPath = href
        ? (href.startsWith('codefile:') ? decodeURIComponent(href.slice('codefile:'.length)) : null)
        : parseFileCitation(children);
      if (citedPath && repoInfo) {
        const filePath = citedPath;
        return (
          <a
            href={href}
            className="text-[var(--link-color)] hover:text-[var(--accent-primary)] border-b border-[var(--link-color)]/35 hover:border-[var(--accent-primary)] no-underline font-medium transition-colors cursor-pointer"
            onClick={(e) => {
              e.preventDefault();
              setOpenCodeFile(filePath);
            }}
            {...props}
          >
            {children}
          </a>
        );
      }
      return (
        <a
          href={href}
          className="text-[var(--link-color)] hover:text-[var(--accent-primary)] border-b border-[var(--link-color)]/35 hover:border-[var(--accent-primary)] no-underline font-medium transition-colors"
          target="_blank"
          rel="noopener noreferrer"
          {...props}
        >
          {children}
        </a>
      );
    },
    blockquote({ children, ...props }: { children?: React.ReactNode }) {
      return (
        <blockquote
          className="border-l-4 border-[var(--accent-primary)] pl-4 py-1 text-[var(--muted)] italic my-4 text-sm bg-[var(--accent-primary)]/5"
          {...props}
        >
          {children}
        </blockquote>
      );
    },
    table({ children, ...props }: { children?: React.ReactNode }) {
      return (
        <div className="overflow-x-auto my-6 rounded-md border border-[var(--border-color)]">
          <table className="min-w-full text-sm border-collapse" {...props}>
            {children}
          </table>
        </div>
      );
    },
    thead({ children, ...props }: { children?: React.ReactNode }) {
      return <thead className="bg-[var(--accent-primary)]/10" {...props}>{children}</thead>;
    },
    tbody({ children, ...props }: { children?: React.ReactNode }) {
      return <tbody className="divide-y divide-[var(--border-color)]" {...props}>{children}</tbody>;
    },
    tr({ children, ...props }: { children?: React.ReactNode }) {
      return <tr className="hover:bg-[var(--accent-primary)]/5 transition-colors" {...props}>{children}</tr>;
    },
    th({ children, ...props }: { children?: React.ReactNode }) {
      return (
        <th
          className="px-4 py-3 text-left font-medium text-[var(--foreground)] font-mono text-xs uppercase tracking-wide"
          {...props}
        >
          {children}
        </th>
      );
    },
    td({ children, ...props }: { children?: React.ReactNode }) {
      return <td className="px-4 py-3 border-t border-[var(--border-color)] text-[var(--foreground)]" {...props}>{children}</td>;
    },
    code(props: {
      inline?: boolean;
      className?: string;
      children?: React.ReactNode;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      [key: string]: any; // Using any here as it's required for ReactMarkdown components
    }) {
      const { inline, className, children, ...otherProps } = props;
      const match = /language-(\w+)/.exec(className || '');
      const codeContent = children ? String(children).replace(/\n$/, '') : '';

      // Handle Mermaid diagrams
      if (!inline && match && match[1] === 'mermaid') {
        return (
          <div className="my-8 bg-[var(--background)]/50 border border-[var(--border-color)] rounded-md overflow-hidden shadow-sm">
            <Mermaid
              chart={codeContent}
              className="w-full max-w-full"
              zoomingEnabled={true}
            />
          </div>
        );
      }

      // Handle code blocks
      if (!inline && match) {
        return (
          <div className="my-6 rounded-md overflow-hidden text-sm shadow-sm border border-[var(--border-color)] border-l-2 border-l-[var(--accent-primary)]">
            <div className="bg-[var(--background)] text-[var(--muted)] px-5 py-2 text-xs font-mono flex justify-between items-center border-b border-[var(--border-color)]">
              <span className="uppercase tracking-wide">{match[1]}</span>
              <button
                onClick={() => {
                  navigator.clipboard.writeText(codeContent);
                }}
                className="text-[var(--muted)] hover:text-[var(--accent-primary)] transition-colors"
                title="Copy code"
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  className="h-5 w-5"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"
                  />
                </svg>
              </button>
            </div>
            <SyntaxHighlighter
              language={match[1]}
              style={tomorrow}
              className="!text-sm"
              customStyle={{ margin: 0, borderRadius: '0 0 0.375rem 0.375rem', padding: '1rem' }}
              showLineNumbers={true}
              wrapLines={true}
              wrapLongLines={true}
              {...otherProps}
            >
              {codeContent}
            </SyntaxHighlighter>
          </div>
        );
      }

      // Handle inline code
      return (
        <code
          className={`${className} font-mono bg-[var(--accent-primary)]/8 border border-[var(--border-color)] px-1.5 py-0.5 rounded text-[var(--accent-primary)] text-sm`}
          {...otherProps}
        >
          {children}
        </code>
      );
    },
  };

  return (
    <div className="prose max-w-none px-2 py-4">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeRaw, rehypeKatex]}
        components={MarkdownComponents}
        // react-markdown's default urlTransform strips any URL scheme not
        // in its own safe-protocol allowlist (http/https/irc/mailto/xmpp)
        // -- silently rewriting a `codefile:<path>` citation's href to ""
        // before the `a` component above ever sees it, which is why
        // clicking one fell through to the plain-link branch and (with an
        // empty href + target="_blank") reopened the current page in a new
        // tab instead of the code viewer. Allowlist codefile: specifically;
        // everything else still goes through the default sanitizer.
        urlTransform={(url) => (url.startsWith('codefile:') ? url : defaultUrlTransform(url))}
      >
        {content}
      </ReactMarkdown>
      {openCodeFile && repoInfo && (
        <CodeViewer
          filePath={openCodeFile}
          repoInfo={repoInfo}
          onClose={() => setOpenCodeFile(null)}
        />
      )}
    </div>
  );
};

export default Markdown;