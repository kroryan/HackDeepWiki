/**
 * Maps a file path's extension (or exact basename, for extension-less files
 * like `Dockerfile`) to a Prism language identifier, for the syntax
 * highlighter already used throughout the app (react-syntax-highlighter's
 * Prism build, see Markdown.tsx and CodeViewer.tsx) -- no new dependency,
 * just teaching the existing highlighter what language a given file is.
 * Falls back to 'text' (no highlighting, still renders fine) for anything
 * not in this table rather than guessing wrong.
 */
const EXTENSION_TO_LANGUAGE: Record<string, string> = {
  js: 'javascript', jsx: 'jsx', mjs: 'javascript', cjs: 'javascript',
  ts: 'typescript', tsx: 'tsx',
  py: 'python', pyi: 'python', pyw: 'python',
  go: 'go',
  rs: 'rust',
  java: 'java', kt: 'kotlin', kts: 'kotlin', scala: 'scala', groovy: 'groovy',
  c: 'c', h: 'c',
  cpp: 'cpp', cc: 'cpp', cxx: 'cpp', hpp: 'cpp', hh: 'cpp', hxx: 'cpp',
  cs: 'csharp',
  rb: 'ruby', erb: 'erb',
  php: 'php',
  swift: 'swift',
  m: 'objectivec', mm: 'objectivec',
  sh: 'bash', bash: 'bash', zsh: 'bash', fish: 'bash',
  ps1: 'powershell', psm1: 'powershell',
  bat: 'batch', cmd: 'batch',
  sql: 'sql',
  html: 'markup', htm: 'markup', xml: 'markup', svg: 'markup', vue: 'markup',
  css: 'css', scss: 'scss', sass: 'sass', less: 'less',
  json: 'json', json5: 'json', jsonc: 'json',
  yaml: 'yaml', yml: 'yaml',
  toml: 'toml', ini: 'ini', cfg: 'ini', conf: 'ini',
  md: 'markdown', mdx: 'markdown',
  dockerfile: 'docker',
  makefile: 'makefile', mk: 'makefile',
  graphql: 'graphql', gql: 'graphql',
  proto: 'protobuf',
  lua: 'lua',
  r: 'r',
  dart: 'dart',
  ex: 'elixir', exs: 'elixir',
  erl: 'erlang',
  hs: 'haskell',
  clj: 'clojure', cljs: 'clojure',
  elm: 'elm',
  jl: 'julia',
  nim: 'nim',
  zig: 'zig',
  sol: 'solidity',
  vim: 'vim',
  diff: 'diff', patch: 'diff',
  env: 'bash',
};

// Exact-basename matches for extension-less files, checked before falling
// back to the extension table.
const BASENAME_TO_LANGUAGE: Record<string, string> = {
  dockerfile: 'docker',
  makefile: 'makefile',
  gemfile: 'ruby',
  rakefile: 'ruby',
  '.gitignore': 'text',
  '.env': 'bash',
};

export function getLanguageFromPath(path: string): string {
  const basename = (path.split('/').pop() || path).toLowerCase();
  if (BASENAME_TO_LANGUAGE[basename]) return BASENAME_TO_LANGUAGE[basename];

  const dotIdx = basename.lastIndexOf('.');
  if (dotIdx <= 0) return 'text'; // no extension, or a dotfile with no further dot
  const ext = basename.slice(dotIdx + 1);
  return EXTENSION_TO_LANGUAGE[ext] || 'text';
}
