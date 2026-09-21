import { expect, test } from "@playwright/test";
import { CONFIRM_COMMITTED_SSE, MANIPULATION_AWAITING_CONFIRMATION_SSE, mockSse } from "./fixtures/sse";

test("a manipulation reaches confirmation, and confirming commits it", async ({ page }) => {
  await mockSse(page, "/chat", MANIPULATION_AWAITING_CONFIRMATION_SSE);
  await page.goto("/");
  await expect(page.getByPlaceholder(/ask about the data/i)).toBeVisible();

  await page.getByPlaceholder(/ask about the data/i).fill("Cancel order 42");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.getByText("Confirm this change?")).toBeVisible();
  await expect(page.getByText("UPDATE on orders")).toBeVisible();
  await expect(page.getByText("1 row(s) affected")).toBeVisible();

  await mockSse(page, "/confirm", CONFIRM_COMMITTED_SSE);
  await page.getByRole("button", { name: "Confirm", exact: true }).click();

  await expect(page.getByText("Done - 1 row(s) affected in orders.")).toBeVisible();
  await expect(page.getByText("Confirm this change?")).not.toBeVisible();
});
