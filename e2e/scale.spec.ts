import { expect, test } from '@playwright/test';
import { addItem, go, pickItem, seedItems, signIn } from './helpers';

/**
 * What the console does when the warehouse is not a demo. Measured on a seeded
 * 10,000-item catalogue, the inventory table took 7.4 seconds to appear and put
 * 130,287 nodes in the document, and the item dialog's select took 2.5 seconds
 * to open and another 1.1 to register a choice. Both were rendering the whole
 * catalogue for a screen that shows a few dozen rows of it.
 */
test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('the pager stays out of the way while the whole table fits on one page', async ({ page }) => {
  await go(page, 'Inventory');

  await expect(page.locator('tbody tr')).toHaveCount(8);
  await expect(page.locator('.pager')).toHaveCount(0);
});

test('a catalogue longer than a page is shown a page at a time', async ({ page }) => {
  test.setTimeout(120_000);
  await seedItems(page, 45, 'Bulk');

  const rows = page.locator('tbody tr');
  await expect(rows).toHaveCount(50);
  await expect(page.locator('.pager')).toContainText('Showing 1–50 of 53 items');
  await expect(page.locator('.pager button:has-text("Previous")')).toBeDisabled();

  await page.click('.pager button:has-text("Next")');

  await expect(rows).toHaveCount(3);
  await expect(page.locator('.pager')).toContainText('51–53 of 53 items');
  await expect(page.locator('.pager button:has-text("Next")')).toBeDisabled();

  // Narrowing from the last page must not leave the reader looking at an empty
  // table past the end of what is left.
  await page.fill('.table-tools input', 'Bulk 01');
  await expect(rows).toHaveCount(10);
  await expect(page.locator('.pager')).toHaveCount(0);
});

test('the item dialog offers a handful of matches rather than the catalogue', async ({ page }) => {
  // Nine items against a picker that shows eight: enough to prove it stops.
  await addItem(page, 'Strapping tape', 'B-01-01');
  await page.click('button:has-text("Issue stock")');

  await expect(page.locator('.picker-results li')).toHaveCount(8);
  await expect(page.locator('.picker-hint')).toContainText('8 of 9 matches shown');

  await page.fill('.picker .picker-search', 'strapping');

  await expect(page.locator('.picker-results li')).toHaveCount(1);
  await expect(page.locator('.picker-hint')).toHaveCount(0);
});

test('the picker finds an item by the aisle code printed on the rack', async ({ page }) => {
  await page.click('button:has-text("Issue stock")');
  await page.fill('.picker .picker-search', 'D-01');

  await expect(page.locator('.picker-results li')).toHaveCount(1);
  await expect(page.locator('.picker-results li')).toContainText('Disinfecting wipes');
});

test('a dialog cannot be submitted until an item has been picked', async ({ page }) => {
  // A hidden input carries the choice to the form, and a hidden input cannot
  // be `required` -- so the guard the select used to provide is this button.
  await page.click('button:has-text("Issue stock")');
  const record = page.locator('.modal button:has-text("Record issue")');

  await expect(record).toBeDisabled();

  await pickItem(page, 'Wireless mouse');

  await expect(record).toBeEnabled();
});

test('a search that matches nothing says so instead of showing the first eight', async ({ page }) => {
  await page.click('button:has-text("Issue stock")');
  await page.fill('.picker .picker-search', 'forklift');

  await expect(page.locator('.picker-results li')).toHaveCount(1);
  await expect(page.locator('.picker-none')).toBeVisible();
});

test('the purchase guidance is fetched for the item picked, and follows it', async ({ page }) => {
  // The ranked plan runs to ~1,700 lines at 10,000 items, so the dialog is no
  // longer handed it to search; it asks for the one line it needs. The unit is
  // what gives each item's guidance away.
  await go(page, 'Procurement');
  await page.click('button:has-text("Receive purchase")');

  await pickItem(page, '4 x 6 shipping labels');
  await expect(page.locator('.supplier-advice')).toContainText('rolls');

  await pickItem(page, 'A4 copy paper');
  await expect(page.locator('.supplier-advice')).toContainText('reams');
  await expect(page.locator('.supplier-advice')).not.toContainText('rolls');
});
