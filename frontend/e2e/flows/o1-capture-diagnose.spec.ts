import { test, expect } from '../support/test';
import { seed, showPicker } from '../contact-sheet/document-owner-supervisor';

test('capture trigger at 641 pixels', async ({ page, request }) => {
  await page.setViewportSize({ width: 641, height: 900 });
  await seed(page, request);
  console.log('TRIGGERS', JSON.stringify(await page.locator('.document-header').evaluate(header => ({ canvas: header.closest('.document-canvas')?.getBoundingClientRect().width, identity: [...header.querySelectorAll('button.identity')].map(el => ({ text: el.textContent, display: getComputedStyle(el).display, rect: el.getBoundingClientRect().toJSON() })), details: [...header.querySelectorAll('button.details-toggle')].map(el => ({ text: el.textContent, display: getComputedStyle(el).display, rect: el.getBoundingClientRect().toJSON() })) }))));
  await showPicker(page);
  await expect(page.getByRole('combobox', { name: 'Supervisor', exact: true })).toBeVisible();
});
