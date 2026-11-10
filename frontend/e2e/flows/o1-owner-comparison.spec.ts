import { test, expect } from '../support/test';
import { seed } from '../contact-sheet/document-owner-supervisor';
import { setDebugVersionOverlay } from '../support/heldComments';

const out = '/Users/jarvis/Corsia/projects/corsia/.threads/a8f17a81-7687-4d22-8b07-819464800949/o1-pack/round2-frames';

test('owner slots in the base comparison fixture', async ({ page, request }) => {
  test.setTimeout(180_000);
  await page.setViewportSize({ width: 1025, height: 900 });
  await setDebugVersionOverlay(request, false);
  const open = await seed(page, request);
  for (const width of [1025, 1280]) {
    const target = width === 1025 ? page : await page.context().newPage();
    if (width !== 1025) {
      await target.setViewportSize({ width, height: 900 });
      await open(target);
    }
    const details = target.getByRole('button', { name: 'Document details' });
    if (await details.isVisible() && await details.getAttribute('aria-expanded') !== 'true') await details.click();
    await expect(target.locator('.cm-content')).toBeVisible();
    await target.screenshot({ path: `${out}/o1-details-${width}.png`, animations: 'disabled' });
    const measurements = await target.locator('.document-header').evaluate(header => {
      const box = (el: Element | null) => el?.getBoundingClientRect().toJSON();
      const describe = (selector: string) => {
        const el = header.querySelector<HTMLElement>(selector);
        if (!el) return null;
        const style = getComputedStyle(el);
        return { box: box(el), font: style.font, padding: style.padding, gap: style.gap, scrollWidth: el.scrollWidth, clientWidth: el.clientWidth };
      };
      return { header: box(header), identity: box(header.querySelector('.document-identity')), details: box(header.querySelector('.identity-details')), tools: box(header.querySelector('.document-tools')), metadata: box(header.querySelector('.document-metadata')), age: describe('.identity-age'), owner: describe('.identity-owner'), verified: describe('.identity-verified') };
    });
    console.log('O1', width, JSON.stringify(measurements));
  }
});
