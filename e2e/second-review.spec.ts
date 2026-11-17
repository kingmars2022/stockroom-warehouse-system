import { expect, test } from '@playwright/test';
import { ADMIN, go, pickItem, signIn } from './helpers';

/**
 * Things a second read of the console turned up. None of them is a feature:
 * each is a place where the screen offered something the API would refuse, or
 * kept something that belonged to whoever was signed in before.
 */
test.beforeEach(async ({ page }) => {
  await signIn(page);
});

async function submitClaim(page: import('@playwright/test').Page, purpose: string) {
  await go(page, 'Reimbursements');
  await page.click('button:has-text("Submit expense")');
  await pickItem(page, 'A4 copy paper');
  await page.fill('input[name="supplier"]', 'Corner Stationery');
  await page.fill('input[name="qty"]', '1');
  await page.fill('input[name="amount"]', '12.00');
  await page.fill('input[name="purpose"]', purpose);
  await page.setInputFiles('input[name="receipt"]', { name: 'receipt.jpg', mimeType: 'image/jpeg', buffer: Buffer.from('receipt') });
  await page.click('.modal button:has-text("Submit for approval")');
  await expect(page.locator('.modal')).toHaveCount(0);
}

test('an approver is not offered their own claim to approve', async ({ page }) => {
  await submitClaim(page, 'Printer ran dry');

  const mine = page.locator('tbody tr', { hasText: 'Printer ran dry' });
  await expect(mine).toContainText('Needs another reviewer');
  await expect(mine.getByRole('button', { name: 'Approve reimbursement' })).toHaveCount(0);
  await expect(mine.getByRole('button', { name: 'Reject reimbursement' })).toHaveCount(0);
});

test('somebody else\'s claim can still be approved', async ({ page }) => {
  await go(page, 'Reimbursements');

  const theirs = page.locator('tbody tr', { hasText: 'Urgent replacement for meeting room' });
  await expect(theirs).not.toContainText('Needs another reviewer');
  await theirs.getByRole('button', { name: 'Approve reimbursement' }).click();
  await expect(theirs.getByRole('button', { name: 'Mark paid' })).toBeVisible();
});

test('a demo receipt is a label, not a link to a file that was never stored', async ({ page }) => {
  await go(page, 'Reimbursements');

  const receipt = page.locator('tbody tr', { hasText: 'Urgent replacement for meeting room' }).locator('.file-name');
  await expect(receipt).toHaveText('tech-store-receipt.jpg');
  await expect(receipt).toHaveJSProperty('tagName', 'SPAN');
});

test('signing out does not hand the next person the last one\'s search', async ({ page }) => {
  await go(page, 'Inventory');
  await page.fill('.table-tools input', 'mouse');
  await expect(page.locator('tbody tr')).toHaveCount(1);

  await page.getByRole('button', { name: 'Sign out' }).click();
  await page.fill('input[type="email"]', 'supervisor@stockroom.test');
  await page.fill('input[type="password"]', ADMIN.password);
  await page.click('button:has-text("Sign in")');
  await go(page, 'Inventory');

  await expect(page.locator('.table-tools input')).toHaveValue('');
  await expect(page.locator('tbody tr')).not.toHaveCount(1);
});

test('a second notice is not cut short by the first one\'s timer', async ({ page }) => {
  await page.clock.install();
  await go(page, 'Settings');
  const company = page.locator('section.panel', { hasText: 'New users and people following the default' });
  await company.locator('.segmented button:has-text("Dark")').click();
  await expect(page.locator('.toast')).toBeVisible();
  await page.clock.fastForward(2000);
  await company.locator('.segmented button:has-text("Light")').click();
  // 2.8 seconds after the first notice: its timer used to fire here and take
  // the second notice down 0.8 seconds into its life.
  await page.clock.fastForward(1500);

  await expect(page.locator('.toast')).toBeVisible();
  await page.clock.fastForward(2000);
  await expect(page.locator('.toast')).toHaveCount(0);
});
