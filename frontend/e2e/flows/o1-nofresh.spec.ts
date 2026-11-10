import { test, expect } from '../support/test';
import { seed } from '../contact-sheet/document-owner-supervisor';
import { setDebugVersionOverlay } from '../support/heldComments';
test('ownership without freshness keeps its slot', async ({ page, request }) => {
  test.setTimeout(180_000);
  await page.setViewportSize({ width: 375, height: 900 });
  await setDebugVersionOverlay(request, false);
  await page.context().route('**/api/files/{content,revision}**', async route => {
    const response = await route.fetch();
    const data = await response.json();
    if ('freshness' in data) data.freshness = null;
    await route.fulfill({ response, json: data });
  });
  const open = await seed(page, request);
  await expect(page.getByRole('button', { name: 'Document details' })).toHaveCount(0);
  const style = await page.addStyleTag({ content: '.identity-age { width: 100px !important; }' });
  await expect(page.locator('.identity-owner')).toBeVisible();
  await page.locator('.identity-owner button.identity').click();
  await expect(page.getByRole('combobox', { name: 'Owner', exact: true })).toBeVisible();
  await page.screenshot({ path: '/Users/jarvis/Corsia/projects/corsia/.threads/a8f17a81-7687-4d22-8b07-819464800949/o1-pack/round2-frames/narrow-no-freshness.png', animations: 'disabled' });
  await page.keyboard.press('Escape');
  await style.evaluate(el => el.remove());
  const wide = await page.context().newPage();
  await wide.setViewportSize({ width: 1600, height: 900 });
  await open(wide);
  console.log('WIDE', JSON.stringify(await wide.locator('.document-header').evaluate(header => ({ canvas: header.closest('.document-canvas')?.getBoundingClientRect().toJSON(), metadataDisplay: getComputedStyle(header.querySelector('.document-metadata')!).display }))));
  await expect(wide.locator('.document-metadata button.identity:visible')).toHaveCount(1);
  await expect(wide.locator('.document-metadata time, .document-metadata .metadata-secondary')).toHaveCount(0);
});
