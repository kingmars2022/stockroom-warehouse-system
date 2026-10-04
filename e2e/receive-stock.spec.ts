import { expect, test } from '@playwright/test';
import { go, pickItem, signIn } from './helpers';

/**
 * Receiving stock without a purchase — a return, a transfer in, a correction.
 * The dialog existed and was rendered on a state nothing ever set, so none of
 * this was reachable and none of it had ever run. These cover the path end to
 * end rather than just the button being present.
 */
test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('the receive dialog opens and is told apart from receiving a purchase', async ({ page }) => {
  await page.click('button:has-text("Receive stock")');

  const modal = page.locator('.modal');
  await expect(modal).toContainText('Receive stock');
  // The purchase dialog is the one that asks for a cost; this one must not.
  await expect(modal).not.toContainText('Unit cost');
  await expect(modal).toContainText('Supplier / source');
});

test('it says where to put the stock away, not where to pick it', async ({ page }) => {
  await page.click('button:has-text("Receive stock")');
  await pickItem(page, 'USB-C charging cable');

  await expect(page.locator('.pick-path')).toContainText('Put away at');
  await expect(page.locator('.pick-path')).toContainText('Aisle A · Bay 01 · Level 02');
  await expect(page.locator('.pick-path')).not.toContainText('Pick from');
});

test('receiving adds to the quantity on hand and records who it came from', async ({ page }) => {
  await go(page, 'Inventory');
  const row = page.locator('tbody tr', { hasText: 'USB-C charging cable' }).first();
  const before = Number((await row.locator('td').nth(2).innerText()).match(/\d+/)![0]);

  await page.click('button:has-text("Receive stock")');
  await pickItem(page, 'USB-C charging cable');
  await page.fill('input[name="qty"]', '14');
  await page.fill('input[name="recipient"]', 'Northstar Supply');
  await page.fill('input[name="note"]', 'Returned from the Design team');
  await page.click('.modal button:has-text("Record receipt")');
  await expect(page.locator('.modal')).toHaveCount(0);

  await go(page, 'Inventory');
  await expect(row.locator('td').nth(2)).toContainText(String(before + 14));

  // The relational log words an arrival as coming *from* somewhere. The demo
  // store used to say "received to", which only showed once this was reachable.
  await go(page, 'Audit log');
  await expect(page.locator('table').last()).toContainText('received from Northstar Supply');
});

test('an employee is not offered a button the API would refuse', async ({ page }) => {
  // Signed in as the admin from beforeEach: establish the button exists at all,
  // so the absence asserted below is the role gate and not a missing feature.
  await expect(page.locator('button:has-text("Receive stock")')).toHaveCount(1);

  await page.click('button[aria-label="Sign out"]');
  await page.fill('input[type="email"]', 'employee@stockroom.test');
  await page.fill('input[type="password"]', 'Stockroom!2026');
  await page.click('button:has-text("Sign in")');
  await expect(page.getByText(/Overview|Welcome back/i).first()).toBeVisible();

  await expect(page.locator('button:has-text("Receive stock")')).toHaveCount(0);
  // Issuing is still theirs to do.
  await expect(page.locator('button:has-text("Issue stock")')).toHaveCount(1);
});
