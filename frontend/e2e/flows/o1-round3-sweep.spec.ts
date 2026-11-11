import { mkdirSync } from 'node:fs';
import { test, expect } from '../support/test';
import { seed } from '../contact-sheet/document-owner-supervisor';
import { setDebugVersionOverlay } from '../support/heldComments';

const out = '/Users/jarvis/Corsia/projects/corsia/.threads/a8f17a81-7687-4d22-8b07-819464800949/o1-pack/round3-frames';
for (const workspace of ['closed', 'open']) test(`round 3 widths, Workspace ${workspace}`, async ({ page, request }) => {
  test.setTimeout(900_000);
  mkdirSync(out, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await setDebugVersionOverlay(request, false);
  await seed(page, request);
  if (workspace === 'closed') await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
  const missing: string[] = [];
  for (const root of [15, 16, 17]) {
    await page.evaluate(size => document.documentElement.style.fontSize = `${size}px`, root);
    for (let width = 320; width <= 1440; width += 20) {
      await page.setViewportSize({ width, height: 900 });
      await page.waitForTimeout(300);
      const info = await page.locator('.document-header').evaluate(header => {
        const control = [...header.querySelectorAll<HTMLElement>('button.identity')].find(e => e.getBoundingClientRect().width > 0 && getComputedStyle(e).visibility !== 'hidden');
        const rect = control?.getBoundingClientRect();
        const parts = [...(control?.querySelectorAll<HTMLElement>('.identity-part') ?? [])].map(e => ({ text: e.innerText, rect: e.getBoundingClientRect().toJSON(), name: e.querySelector<HTMLElement>('.identity-name')?.getBoundingClientRect().toJSON() }));
        const tools = header.querySelector<HTMLElement>('.document-tools');
        const metadata = header.querySelector<HTMLElement>('.document-metadata');
        return { rect: rect?.toJSON(), parts, header: header.getBoundingClientRect().toJSON(), headerClient: header.clientWidth, headerScroll: header.scrollWidth, tools: tools?.getBoundingClientRect().toJSON(), metadata: metadata?.getBoundingClientRect().toJSON() };
      });
      console.log('SWEEP', workspace, root, width, JSON.stringify(info));
      await page.screenshot({ path: `${out}/${workspace}-${root}-${width}.png`, clip: { x: 0, y: 0, width, height: 240 }, animations: 'disabled' });
      if ((!info.rect || !info.parts.length) && !(workspace === 'open' && width >= 1025 && width <= 1372)) missing.push(`${workspace}/${root}/${width}`);
    }
  }
  expect(missing, 'widths without a visible identity').toEqual([]);
});
