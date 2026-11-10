import { test, expect } from '../support/test';
import { seed } from '../contact-sheet/document-owner-supervisor';

test('ownership slot switches without a gap at the canvas boundary', async ({ page, request }) => {
  test.setTimeout(180_000);
  await page.setViewportSize({ width: 641, height: 900 });
  await seed(page, request);
  await expect(page.locator('button.identity').first()).toBeAttached();
  await expect(page.locator('button.identity:visible, button.details-toggle:visible').first()).toBeVisible();
  console.log('VIEWPORT 641', await page.locator('.document-canvas').evaluate(el => ({ border: el.getBoundingClientRect().width, content: getComputedStyle(el).width })));
  await page.screenshot({ path: '/Users/jarvis/Corsia/projects/corsia/.threads/a8f17a81-7687-4d22-8b07-819464800949/o1-pack/round2-frames/o1-641.png', animations: 'disabled' });
  for (const root of [15, 16, 17]) {
    await page.evaluate(value => { document.documentElement.style.fontSize = `${value}px`; }, root);
    for (const delta of [-2, -1, 0, 1, 2]) {
      const width = root * 37.5 + delta;
      await page.locator('.document-canvas').evaluate((el: HTMLElement, w) => { el.style.boxSizing = 'content-box'; el.style.width = `${w}px`; el.style.minWidth = '0'; el.style.maxWidth = 'none'; el.style.flex = 'none'; }, width);
      const state = await page.locator('.document-header').evaluate(header => {
        const visible = (el: Element | null) => !!el && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0 && getComputedStyle(el).visibility !== 'hidden';
        const canvas = header.closest('.document-canvas')!;
        return { content: getComputedStyle(canvas).width, metadata: visible(header.querySelector('.document-metadata button.identity')), compact: visible(header.querySelector('.identity-owner button.identity')), details: visible(header.querySelector('button.details-toggle')) };
      });
      console.log('BOUNDARY', JSON.stringify({ root, delta, ...state }));
      expect(Number(state.metadata) + Number(state.compact || state.details)).toBe(1);
    }
  }
});
