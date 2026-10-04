import { expect, test } from '@playwright/test';
import { go, pickItem, signIn } from './helpers';

/**
 * Price alerts come from the server now. They used to be the console filtering
 * every purchase it had been sent, and the alternative prices beside each one
 * came from scanning that same list — neither of which a page of history can
 * answer. The demo store derives them the same way the API does, so these drive
 * what the panel actually says and what the dashboard counts.
 */
test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test('an alert names the supplier that charged more and what the others charge', async ({ page }) => {
  await go(page, 'Procurement');
  const alerts = page.locator('.alert-row');

  await expect(alerts).toHaveCount(2);
  await expect(alerts.filter({ hasText: 'USB-C charging cable' })).toContainText('Northstar Supply charged $3.60');
  // The latest price from the other supplier of the same item, named — not a
  // bare figure, which is useless for deciding who to buy from next.
  await expect(alerts.filter({ hasText: 'USB-C charging cable' })).toContainText('Atlas Office Goods $3.00');
  await expect(alerts.filter({ hasText: '4 x 6 shipping labels' })).toContainText('ClearLane Distribution $10.55');
});

test('an alert never quotes the supplier it is complaining about', async ({ page }) => {
  await go(page, 'Procurement');
  const alert = page.locator('.alert-row').filter({ hasText: 'USB-C charging cable' });

  const alternatives = (await alert.locator('small').innerText()).split('Alternative recent prices:')[1];

  expect(alternatives).not.toContain('Northstar');
});

test('the dashboard counts every alert, and the threshold decides what is one', async ({ page }) => {
  const card = page.locator('.metric', { hasText: 'Price alerts' });
  await expect(card.locator('strong')).toHaveText('2');

  // 18.4% is an alert at 15% and not at 19%.
  await go(page, 'Procurement');
  await page.fill('.threshold input', '19');
  await page.locator('.threshold input').blur();
  await expect(page.locator('.alert-row')).toHaveCount(1);

  await go(page, 'Overview');
  await expect(card.locator('strong')).toHaveText('1');
});

test('a first purchase of an item is a first price, not a price rise', async ({ page }) => {
  await go(page, 'Procurement');
  await page.click('button:has-text("Receive purchase")');
  await pickItem(page, 'Laptop stand');
  await page.selectOption('select[name="supplier"]', { label: 'Northstar Supply' });
  await page.fill('input[name="qty"]', '10');
  await page.fill('input[name="unitCost"]', '99.99');
  await page.fill('input[name="invoice"]', 'INV-7000');
  await page.click('.modal button:has-text("Receive and record cost")');
  await expect(page.locator('.modal')).toHaveCount(0);

  // Nothing to compare 99.99 against, so the row reads as a first price and
  // the alert list is the two it already was -- an alert is a rise, not a
  // high number.
  await expect(page.locator('.table-wrap tbody tr').first()).toContainText('First price');
  await expect(page.locator('.alert-row')).toHaveCount(2);
  await expect(page.locator('.alert-row').filter({ hasText: 'Laptop stand' })).toHaveCount(0);
});
