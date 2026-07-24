import { defineConfig } from 'vitest/config';
import { resolve } from 'path';

// Vitest config for the pure-logic utils (streamParser, mermaid normalize).
// These modules are framework/DOM-free, so the default node environment is
// enough -- no jsdom/happy-dom dependency to install or maintain. Path alias
// mirrors next.config/tsconfig so `@/` imports resolve the same way as in the
// app (a few utils import via `@/contexts/...`).
export default defineConfig({
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  // The app's postcss.config.mjs uses the Tailwind v4 plugin, which Vite can't
  // load outside the Next.js pipeline (it throws "Invalid PostCSS Plugin").
  // These tests are pure TS with no CSS, so pin postcss to an empty config --
  // this makes Vite skip its postcss.config.* file search entirely instead of
  // failing to load the Tailwind plugin.
  css: { postcss: { plugins: [] } },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});