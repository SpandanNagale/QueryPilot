import { expect, test } from "@playwright/test";

// Real backend, no mocking: /session/reset and /tables are fast, plain
// SQLite operations with no LLM call involved, so there's no reason to
// mock them for a smoke test -- this genuinely exercises Phase 2a's
// per-session sandbox provisioning end to end.
test("resetting the sandbox gets a new session id and a fresh copy of the data", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByPlaceholder(/ask about the data/i)).toBeVisible();

  const sessionIdLocator = page.getByText(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  await expect(sessionIdLocator).toBeVisible();
  const before = await sessionIdLocator.textContent();

  await expect(page.getByText("products").first()).toBeVisible();

  await page.getByRole("button", { name: "Reset sandbox" }).click();

  await expect(async () => {
    const after = await sessionIdLocator.textContent();
    expect(after).not.toBe(before);
  }).toPass({ timeout: 10_000 });

  // The table browser re-fetches against the new session and still works.
  await expect(page.getByText("products").first()).toBeVisible();
});
