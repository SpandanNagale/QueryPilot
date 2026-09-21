import { defineConfig, devices } from "@playwright/test";

/** Smoke tests run against a REAL locally-started backend (see
 * `webServer` below, and .github/workflows for CI) for every fast,
 * no-LLM-call endpoint (/health, /session/reset, /tables, /health/*).
 * The LLM-driven /chat and /confirm SSE streams are intercepted with
 * canned, deterministic responses (see tests/fixtures/sse.ts) so these
 * tests exercise the frontend's own event-handling logic quickly and
 * reliably, rather than real (slow, sometimes flaky free-tier) provider
 * calls -- that correctness is evals/run_evals.py's job, not this one's. */
export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "npm run dev",
      url: "http://localhost:3000",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
    {
      // Plain `python`, not a hardcoded venv path -- portable between this
      // Windows dev machine (reuseExistingServer skips spawning it here
      // when a backend's already running) and the Linux CI runner, where
      // `python` resolves to whatever actions/setup-python configured.
      command: "cd .. && python -m uvicorn app.main:app --host 127.0.0.1 --port 8000",
      url: "http://localhost:8000/health",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
      env: { QP_CORS_ALLOWED_ORIGINS: "http://localhost:3000" },
    },
  ],
});
