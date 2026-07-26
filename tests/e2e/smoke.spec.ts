import { expect, test } from '@playwright/test';

test('first-load shell renders without a browser exception', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const response = await page.goto('/');
  expect(response?.ok()).toBeTruthy();
  await expect(page.locator('body')).toBeVisible();
  expect(errors).toEqual([]);
});
