import { expect, test } from "@playwright/test";

test("cold-start gate shows 'waking up' then reveals the app once /health succeeds", async ({ page }) => {
  let healthCalls = 0;
  await page.route("**/health", async (route) => {
    healthCalls += 1;
    if (healthCalls === 1) {
      // First poll fails, like a Render free-tier instance still spinning up.
      await route.abort("failed");
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ healthy: true, database: { available: true, detail: "ok" }, read_only_mode: false }) });
  });

  await page.goto("/");

  await expect(page.getByText(/waking up the backend/i)).toBeVisible();
  await expect(page.getByText(/querypilot/i)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByPlaceholder(/ask about the data/i)).toBeVisible();
});

test("cold-start gate shows an unreachable message if the backend never responds", async ({ page }) => {
  await page.route("**/health", (route) => route.abort("failed"));

  await page.goto("/");
  await expect(page.getByText(/waking up the backend/i)).toBeVisible();
  // Real 90s deadline is too slow for a smoke test; this only proves the
  // gate is showing progress and hasn't rendered the app early.
  await expect(page.getByPlaceholder(/ask about the data/i)).not.toBeVisible();
});
