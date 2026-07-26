import { defineConfig, globalIgnores } from 'eslint/config';
import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTypeScript from 'eslint-config-next/typescript';

export default defineConfig([
  ...nextVitals,
  ...nextTypeScript,
  {
    // React Compiler diagnostics are valuable but enabling them as hard
    // errors across the pre-compiler codebase would turn the Next 16 tool
    // upgrade into an unrelated rewrite. New feature modules still follow
    // the hooks rules; these compiler-only gates are introduced gradually.
    rules: {
      'react-hooks/immutability': 'off',
      'react-hooks/preserve-manual-memoization': 'off',
      'react-hooks/purity': 'off',
      'react-hooks/set-state-in-effect': 'off',
    },
  },
  {
    files: ['src/app/[[]owner[]]/[[]repo[]]/page.tsx'],
    rules: {
      // The compatibility setter API returned by useWikiWorkspace is stable,
      // but exhaustive-deps cannot infer stability through a custom hook.
      'react-hooks/exhaustive-deps': 'off',
    },
  },
  globalIgnores([
    '.next/**',
    'AppDir/**',
    'dist/**',
    'api/dist/**',
    'api/build/**',
    'coverage/**',
  ]),
]);
