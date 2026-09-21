import { expect, test } from "@playwright/test";
import { ANALYTICS_SUCCESS_SSE, mockSse } from "./fixtures/sse";

test("an analytics query streams through the trace and renders results", async ({ page }) => {
  await mockSse(page, "/chat", ANALYTICS_SUCCESS_SSE);
  await page.goto("/");
  await expect(page.getByPlaceholder(/ask about the data/i)).toBeVisible();

  await page.getByPlaceholder(/ask about the data/i).fill("Which products are colas?");
  await page.getByRole("button", { name: "Send" }).click();

  // Trace pane shows the pipeline as it completes.
  await expect(page.getByText("SQL generator")).toBeVisible();
  await expect(page.getByText("Query executor")).toBeVisible();

  // Right pane: generated SQL, validator badges, results, insight, followups.
  await expect(page.getByText(/SELECT name FROM products/)).toBeVisible();
  await expect(page.getByText("validated")).toBeVisible();
  await expect(page.getByText("Ratna Cola Classic")).toBeVisible();
  await expect(page.getByText("There are two cola products.")).toBeVisible();
  await expect(page.getByRole("button", { name: "What is the average price?" })).toBeVisible();

  // Run summary at the end.
  await expect(page.getByText(/\d+(\.\d+)?s total/)).toBeVisible();
});
