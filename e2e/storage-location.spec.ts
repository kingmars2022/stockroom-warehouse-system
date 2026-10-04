import { expect, test } from '@playwright/test';
import { addItem, go, locationCell, pickItem, signIn } from './helpers';

/**
 * Storage codes are aisle-bay-level. The code is what is printed on the rack,
 * but it is not what a person reads, so these cover both halves: that a code
 * following the convention is spelled out, and that one which does not is left
 * alone rather than forced into a shape it has not got.
 */
test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('the inventory table spells out the aisle, bay and level', async ({ page }) => {
  await go(page, 'Inventory');
  const cell = locationCell(page, 'USB-C charging cable');

  await expect(cell).toContainText('Aisle A · Bay 01 · Level 02');
  // The raw code stays visible: it is what is on the rack label.
  await expect(cell).toContainText('A-01-02');
});

test('issuing stock says where to pick from', async ({ page }) => {
  await go(page, 'Inventory');
  await page.click('button:has-text("Issue stock")');
  await page.getByText('Issue stock').first().waitFor();

  // Nothing to say until an item is chosen.
  await expect(page.locator('.pick-path')).toHaveCount(0);

  await pickItem(page, 'USB-C charging cable');
  await expect(page.locator('.pick-path')).toContainText('Pick from');
  await expect(page.locator('.pick-path')).toContainText('Aisle A · Bay 01 · Level 02');
});

test('the pick line follows the item, not the first one chosen', async ({ page }) => {
  await go(page, 'Inventory');
  await page.click('button:has-text("Issue stock")');
  await page.getByText('Issue stock').first().waitFor();

  await pickItem(page, 'A4 copy paper');
  await expect(page.locator('.pick-path')).toContainText('Aisle B · Bay 03 · Level 01');

  await pickItem(page, 'Disinfecting wipes');
  await expect(page.locator('.pick-path')).toContainText('Aisle D · Bay 01 · Level 03');
});

test('a location that is not aisle-bay-level is shown exactly as entered', async ({ page }) => {
  await addItem(page, 'Pallet jack', 'Mezzanine, north wall');
  const cell = locationCell(page, 'Pallet jack');

  await expect(cell).toHaveText('Mezzanine, north wall');
  // No invented aisle, and no second line pretending there is a rack code.
  await expect(cell).not.toContainText('Aisle');
});

test('an item with no location reads as unassigned rather than breaking', async ({ page }) => {
  await addItem(page, 'Zip ties, 200mm', '');

  await expect(locationCell(page, 'Zip ties, 200mm')).toHaveText('Unassigned');
});
