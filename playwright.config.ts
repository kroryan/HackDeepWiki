import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 60_000,
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: process.env.HACKDEEPWIKI_E2E_URL || 'http://127.0.0.1:3000',
    trace: 'retain-on-failure',
  },
  webServer: process.env.HACKDEEPWIKI_E2E_EXTERNAL
    ? undefined
    : {
        command: 'npm run dev',
        url: 'http://127.0.0.1:3000',
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
      },
});
