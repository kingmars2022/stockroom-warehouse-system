import { expect, test } from '@playwright/test';
import { go, pickItem, signIn } from './helpers';

/**
 * Every row that is about an item names it, and names its unit. The name used
 * to be looked up against a catalogue the console held in full, which is the
 * one thing a warehouse with 10,000 SKUs cannot be sent; the API joins it onto
 * the row now, and the demo store does the same so one set of components reads
 * both. These drive each of the three tables through a write, because the
 * writers are where a missed field hides — a purchase names two rows, itself
 * and the receipt it causes.
 */
test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('an issue shows up in the activity table named, with its unit', async ({ page }) => {
  await page.click('button:has-text("Issue stock")');
  await pickItem(page, 'USB-C charging cable');
  await page.fill('input[name="qty"]', '7');
  await page.fill('input[name="recipient"]', 'Design team');
  await page.click('.modal button:has-text("Record issue")');
  await expect(page.locator('.modal')).toHaveCount(0);

  await go(page, 'Activity');
  const row = page.locator('tbody tr').first();

  await expect(row).toContainText('USB-C charging cable');
  await expect(row).toContainText('-7 pcs');
});

test('a purchase names its item on the purchase row and on the receipt it causes', async ({ page }) => {
  await go(page, 'Procurement');
  await page.click('button:has-text("Receive purchase")');
  await pickItem(page, 'Wireless mouse');
  await page.fill('input[name="qty"]', '25');
  await page.fill('input[name="unitCost"]', '19.99');
  await page.fill('input[name="invoice"]', 'INV-5150');
  await page.click('.modal button:has-text("Receive and record cost")');
  await expect(page.locator('.modal')).toHaveCount(0);

  const purchase = page.locator('.table-wrap tbody tr').first();
  await expect(purchase).toContainText('Wireless mouse');
  await expect(purchase).toContainText('25 pcs');

  // The inbound movement the receipt records is a second row, written by the
  // same handler, and it needs the name too.
  await go(page, 'Activity');
  const movement = page.locator('tbody tr').first();
  await expect(movement).toContainText('Wireless mouse');
  await expect(movement).toContainText('+25 pcs');
});

test('a reimbursement names the item it was claimed for', async ({ page }) => {
  await go(page, 'Reimbursements');
  await page.click('button:has-text("Submit expense")');
  await pickItem(page, 'A4 copy paper');
  await page.fill('input[name="supplier"]', 'Corner Stationery');
  await page.fill('input[name="qty"]', '2');
  await page.fill('input[name="amount"]', '31.40');
  await page.fill('input[name="purpose"]', 'Ran out before the delivery landed');
  await page.setInputFiles('input[name="receipt"]', { name: 'receipt.jpg', mimeType: 'image/jpeg', buffer: Buffer.from('receipt') });
  await page.click('.modal button:has-text("Submit for approval")');
  await expect(page.locator('.modal')).toHaveCount(0);

  await expect(page.locator('tbody tr').first()).toContainText('A4 copy paper · 2 reams');
});

test('the seeded rows are named too, not only the ones written in this session', async ({ page }) => {
  // nameItems() plays the part of the join for the demo store. Without it
  // every pre-existing row loses its name, which is a different bug from a
  // writer forgetting one.
  await go(page, 'Activity');
  await expect(page.locator('tbody')).toContainText('Shipping box, medium');

  await go(page, 'Reimbursements');
  await expect(page.locator('tbody')).toContainText('Wireless mouse');

  await go(page, 'Procurement');
  await expect(page.locator('.table-wrap tbody')).toContainText('4 x 6 shipping labels');
});
