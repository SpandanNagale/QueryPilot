import { expect, test } from "@playwright/test";
import { CONFIRM_CANCELLED_SSE, MANIPULATION_AWAITING_CONFIRMATION_SSE, mockSse } from "./fixtures/sse";

test("cancelling a manipulation confirmation makes no change", async ({ page }) => {
  await mockSse(page, "/chat", MANIPULATION_AWAITING_CONFIRMATION_SSE);
  await page.goto("/");
  await expect(page.getByPlaceholder(/ask about the data/i)).toBeVisible();

  await page.getByPlaceholder(/ask about the data/i).fill("Cancel order 42");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Confirm this change?")).toBeVisible();

  await mockSse(page, "/confirm", CONFIRM_CANCELLED_SSE);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();

  await expect(page.getByText("Cancelled - no changes were made.")).toBeVisible();
  await expect(page.getByText("Confirm this change?")).not.toBeVisible();
});
