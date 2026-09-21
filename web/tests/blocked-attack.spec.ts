import { expect, test } from "@playwright/test";
import { GUARDRAIL_BLOCKED_SSE, mockSse } from "./fixtures/sse";

test("a 'try to break it' preset shows the guardrail blocking it", async ({ page }) => {
  await mockSse(page, "/chat", GUARDRAIL_BLOCKED_SSE);
  await page.goto("/");
  await expect(page.getByPlaceholder(/ask about the data/i)).toBeVisible();

  // The preset chip itself sends the message -- exercises the same click
  // path a real visitor uses, not just the composer.
  await page.getByRole("button", { name: /ignore all previous instructions/i }).click();

  await expect(page.getByText(/blocked by the input safety filter/i)).toBeVisible();
  await expect(page.getByText(/prompt-injection attempt/i)).toBeVisible();

  // The trace pane shows exactly which layer blocked it: guardrail failed,
  // nothing downstream ran.
  await expect(page.getByText("Failed")).toBeVisible();
  await expect(page.getByText("Not reached").first()).toBeVisible();
});
