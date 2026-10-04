import { Page, expect } from '@playwright/test';

export const ADMIN = { email: 'admin@stockroom.test', password: 'Stockroom!2026' };

export async function signIn(page: Page) {
  await page.goto('/');
  await page.fill('input[type="email"]', ADMIN.email);
  await page.fill('input[type="password"]', ADMIN.password);
  await page.click('button:has-text("Sign in")');
  await expect(page.getByText(/Overview|Welcome back/i).first()).toBeVisible();
}

export async function go(page: Page, tab: string) {
  await page.click(`nav button:has-text("${tab}")`);
}

/** The seeded store resets on reload, so a test that adds an item gets its own. */
export async function addItem(page: Page, name: string, location: string, qty = 5) {
  await go(page, 'Inventory');
  await page.click('button:has-text("New item")');
  await page.getByText('Add inventory item').waitFor();
  await page.fill('input[name="name"]', name);
  if (location) await page.fill('input[name="location"]', location);
  await page.fill('input[name="qty"]', String(qty));
  await page.click('.modal button:has-text("Add item")');
  await expect(page.locator('.modal')).toHaveCount(0);
}

export async function setDarkMode(page: Page) {
  await go(page, 'Settings');
  await page.click('.segmented button:has-text("Dark")');
  await expect(page.locator(':root[data-theme="dark"]')).toHaveCount(1);
}

/** The location cell for a row, found by the item name in the first column. */
export function locationCell(page: Page, itemName: string) {
  return page.locator('tbody tr', { hasText: itemName }).first().locator('td').nth(1);
}
