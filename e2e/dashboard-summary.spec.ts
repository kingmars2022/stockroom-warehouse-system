import { expect, test } from '@playwright/test';
import { addItem, go, signIn } from './helpers';

/**
 * The dashboard's three figures are counted in the database now. They used to
 * be reduced over every item in the warehouse in the browser, which was the
 * last thing keeping the whole catalogue in the console. The demo store
 * derives them the same way `/api/items/summary` does, so these pin both.
 */
test.beforeEach(async ({ page }) => {
  await signIn(page);
});

const card = (page: import('@playwright/test').Page, title: string) =>
  page.locator('.metric', { hasText: title }).locator('strong');

test('the cards count the catalogue, the units in it, and what is short', async ({ page }) => {
  await expect(card(page, 'Stocked items')).toHaveText('8');
  await expect(card(page, 'Units on hand')).toHaveText('429');
  // At or below the minimum: paper, mouse, printer and labels.
  await expect(card(page, 'Needs attention')).toHaveText('4');
});

test('the attention queue leads with the most depleted, not the emptiest shelf', async ({ page }) => {
  // Labels are 9 of a minimum 25, short by 16; paper is 12 of 20, short by 8.
  // Ordering by what is left would put the printer (3 of 5) first.
  const queue = page.locator('.low-row');

  await expect(queue.first()).toContainText('4 x 6 shipping labels');
  await expect(queue.nth(1)).toContainText('A4 copy paper');
});

test('the attention card opens the inventory filtered to what is short', async ({ page }) => {
  await page.click('.metric:has-text("Needs attention")');

  await expect(page.locator('.filter-pill')).toContainText('Low stock only');
  await expect(page.locator('tbody tr')).toHaveCount(4);
  await expect(page.locator('tbody')).not.toContainText('Shipping box, medium');

  await page.click('.filter-pill');

  await expect(page.locator('.filter-pill')).toHaveCount(0);
  await expect(page.locator('tbody tr')).toHaveCount(8);
});

test('the figures follow a write rather than going stale', async ({ page }) => {
  await addItem(page, 'Pallet wrap', 'C-01-01', 40);

  await go(page, 'Overview');

  await expect(card(page, 'Stocked items')).toHaveText('9');
  await expect(card(page, 'Units on hand')).toHaveText('469');
  // 40 on hand against a minimum of 0, so it is not short.
  await expect(card(page, 'Needs attention')).toHaveText('4');
});

test('an item sitting exactly on its minimum counts as needing attention', async ({ page }) => {
  // `<=`, not `<`. A minimum is the level at which to reorder, not the level
  // below which to panic — and no seeded item sits on the line, so this is the
  // only place the boundary is exercised.
  await go(page, 'Inventory');
  await page.click('button:has-text("New item")');
  await page.fill('input[name="name"]', 'Stretch film');
  await page.fill('input[name="qty"]', '5');
  await page.fill('input[name="min"]', '5');
  await page.click('.modal button:has-text("Add item")');
  await expect(page.locator('.modal')).toHaveCount(0);

  await go(page, 'Overview');

  await expect(card(page, 'Needs attention')).toHaveText('5');
});
