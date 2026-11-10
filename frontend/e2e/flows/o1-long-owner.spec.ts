import { test, expect } from '../support/test';
import { seed } from '../contact-sheet/document-owner-supervisor';
import { setDebugVersionOverlay } from '../support/heldComments';

test('long owner remains readable as the row narrows', async ({ page, request }) => {
  test.setTimeout(180_000);
  await page.setViewportSize({ width: 641, height: 900 });
  await setDebugVersionOverlay(request, false);
  await seed(page, request);
  const identity = page.locator('button.identity:visible').first();
  await identity.click();
  await page.getByRole('combobox', { name: 'Owner', exact: true }).click();
  await page.getByRole('option', { name: /A very long agent name.*Paused/ }).click();
  await expect(page.locator('button.identity:visible').first()).toHaveAttribute('title', /Owner: A very long agent name/);
  await page.keyboard.press('Escape');
  for (const width of [641, 899]) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(200);
    await page.screenshot({ path: `/Users/jarvis/Corsia/projects/corsia/.threads/a8f17a81-7687-4d22-8b07-819464800949/o1-pack/round2-frames/long-owner-${width}.png`, animations: 'disabled' });
    console.log('LONG', width, JSON.stringify(await page.locator('.document-header').evaluate(header => [...header.querySelectorAll('button.identity')].map(el => ({ rect: el.getBoundingClientRect().toJSON(), compact: el.classList.contains('compact'), text: el.textContent?.trim() })) )));
  }
  for (let width = 640; width >= 320; width--) {
    await page.setViewportSize({ width, height: 900 });
    const isRowVisible = await page.locator('.identity-owner button.identity').isVisible();
    if (!isRowVisible) {
      await page.setViewportSize({ width: width + 1, height: 900 });
      console.log('NARROWEST ROW VIEWPORT', width + 1, 'CANVAS', await page.locator('.document-canvas').evaluate(el => getComputedStyle(el).width));
      await page.screenshot({ path: '/Users/jarvis/Corsia/projects/corsia/.threads/a8f17a81-7687-4d22-8b07-819464800949/o1-pack/round2-frames/long-owner-narrowest.png', animations: 'disabled' });
      break;
    }
  }
});
