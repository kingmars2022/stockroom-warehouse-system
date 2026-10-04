import { expect, test } from '@playwright/test';

/**
 * A production build, served by `next start`, reaches the console.
 *
 * This is the whole deployable-demo claim in one test. Demo mode used to be
 * gated on NODE_ENV alone, so every build that a host could actually serve
 * stopped at a login nothing could complete — the console was only ever
 * runnable from a developer's own machine. The rest of the suite runs against
 * `next dev` and cannot see that, because NEXT_PUBLIC_* values are inlined by
 * `next build`.
 */
test('the built console signs in and works without a backend', async ({ page }) => {
  await page.goto('/');
  await page.fill('input[type="email"]', 'admin@stockroom.test');
  await page.fill('input[type="password"]', 'Stockroom!2026');
  await page.click('button:has-text("Sign in")');

  await expect(page.getByText(/Overview|Welcome back/i).first()).toBeVisible();
  await expect(page.locator('.metric', { hasText: 'Stocked items' }).locator('strong')).toHaveText('8');

  // Far enough in to prove it is the real console and not a shell: the
  // inventory reads from the seeded store, and the item dialog searches it.
  await page.click('nav button:has-text("Inventory")');
  await expect(page.locator('tbody tr')).toHaveCount(8);

  await page.click('button:has-text("Issue stock")');
  await page.fill('.picker .picker-search', 'labels');
  await expect(page.locator('.picker-results li')).toHaveCount(1);
});
