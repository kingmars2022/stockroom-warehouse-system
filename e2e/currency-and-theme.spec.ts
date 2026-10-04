import { expect, test } from '@playwright/test';
import { go, pickItem, setDarkMode, signIn } from './helpers';

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

/**
 * Money is Canadian. The locale matters as much as the currency code: en-US
 * renders CAD as "CA$12.50", which is how a foreign reader is shown Canadian
 * money, not how a Montreal warehouse writes its own prices. en-CA gives the
 * bare "$12.50" for local money and keeps a prefix for anything else, which is
 * what makes the cross-currency comparison in the price engine legible.
 */
test('prices read as plain dollars, not as a foreign currency', async ({ page }) => {
  await go(page, 'Procurement');
  const table = page.locator('.table-wrap').first();

  await expect(table).toContainText('$12.50');
  await expect(table).not.toContainText('CA$');
  await expect(table).not.toContainText('USD');
});

test('the amount fields ask for Canadian dollars', async ({ page }) => {
  await go(page, 'Procurement');
  await page.click('button:has-text("Receive purchase")');

  await expect(page.locator('.modal')).toContainText('Unit cost (CAD)');
  await expect(page.locator('.modal')).not.toContainText('(USD)');
});

/**
 * The three green callouts share one dark rule. They sit inside panels and
 * modals that go dark, and for a long time they did not, so each stayed a pale
 * block. Asserting the computed background is the only way to catch that
 * coming back: the markup is identical either way.
 */
test('the callouts go dark with the rest of the console', async ({ page }) => {
  const DARK_FILL = 'rgb(23, 37, 29)';

  await setDarkMode(page);
  await go(page, 'Inventory');
  await page.click('button:has-text("Issue stock")');
  await pickItem(page, 'USB-C charging cable');

  await expect(page.locator('.pick-path')).toHaveCSS('background-color', DARK_FILL);
});

test('the purchase guidance keeps the recommended supplier distinct in dark mode', async ({ page }) => {
  await setDarkMode(page);
  await go(page, 'Procurement');
  await page.click('button:has-text("Receive purchase")');
  await pickItem(page, '4 x 6 shipping labels');

  const advice = page.locator('.supplier-advice');
  await expect(advice).toHaveCSS('background-color', 'rgb(23, 37, 29)');

  // Flattening every line to one colour would lose the reason this box exists.
  const recommended = await advice.locator('small.recommended').evaluate(el => getComputedStyle(el).color);
  const alternative = await advice.locator('small:not(.recommended)').first().evaluate(el => getComputedStyle(el).color);
  expect(recommended).not.toBe(alternative);
});
