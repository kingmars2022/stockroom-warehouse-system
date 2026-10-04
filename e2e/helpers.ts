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

/**
 * The item picker is a search box over the catalogue rather than a <select>,
 * because a select holding one option per SKU took 2.5 seconds to open at
 * 10,000 items. Picking is therefore: narrow, then click the row.
 */
export async function pickItem(page: Page, name: string) {
  const chosen = page.locator('.picker-chosen');
  if (await chosen.count()) await chosen.getByRole('button', { name: 'Change' }).click();
  await page.fill('.picker .picker-search', name);
  await page.locator('.picker-results li', { hasText: name }).first().locator('button').click();
  await expect(chosen).toContainText(name);
}

/**
 * Enough items to run the inventory table past one page. The demo store seeds
 * eight and resets on reload, so a test that needs a long catalogue has to
 * build one; only the name has to differ, which keeps it to three actions each.
 */
export async function seedItems(page: Page, count: number, prefix: string) {
  await go(page, 'Inventory');
  for (let index = 0; index < count; index += 1) {
    await page.click('button:has-text("New item")');
    await page.fill('input[name="name"]', `${prefix} ${String(index).padStart(3, '0')}`);
    await page.click('.modal button:has-text("Add item")');
  }
  await expect(page.locator('.modal')).toHaveCount(0);
}
