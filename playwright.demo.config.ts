import { defineConfig, devices } from '@playwright/test';

const PORT = 3101;

/**
 * The deployable demo, tested as it would actually be served.
 *
 * The main suite drives `next dev`, which says nothing about the artifact a
 * host would run: NEXT_PUBLIC_* variables are inlined at build time, so
 * whether a production build reaches demo mode is decided by `next build` and
 * cannot be checked by running the dev server. This config builds it the way
 * a free static host would and serves it with `next start`.
 */
export default defineConfig({
  testDir: './e2e',
  testMatch: /demo-build\.spec\.ts/,
  forbidOnly: !!process.env.CI,
  reporter: 'list',
  use: { baseURL: `http://localhost:${PORT}`, trace: 'on-first-retry' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `NEXT_PUBLIC_DEMO=true npm run build && npx next start --port ${PORT}`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: false,
    timeout: 240_000,
  },
});
