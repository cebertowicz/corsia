import { test, expect } from '../support/test';
import { seed, showPicker } from '../contact-sheet/document-owner-supervisor';

test('capture trigger at 641 pixels', async ({ page, request }) => {
  await page.setViewportSize({ width: 641, height: 900 });
  await seed(page, request);
  await expect(page.locator('button.identity').first()).toBeAttached();
  console.log('TRIGGERS', JSON.stringify(await page.locator('.document-header').evaluate(header => ({ canvas: header.closest('.document-canvas')?.getBoundingClientRect().toJSON(), nodes: [...header.querySelectorAll('.document-identity, .identity-details, .identity-age, .identity-owner, .document-metadata, button.identity, button.details-toggle')].map(el => ({ class: el.className, text: el.textContent?.trim(), display: getComputedStyle(el).display, rect: el.getBoundingClientRect().toJSON() })) }))));
  await showPicker(page);
  await expect(page.getByRole('combobox', { name: 'Supervisor', exact: true })).toBeVisible();
});
