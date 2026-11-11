import { mkdirSync } from 'node:fs';
import { test, expect } from '../support/test';
import { seed } from '../contact-sheet/document-owner-supervisor';
import { setDebugVersionOverlay } from '../support/heldComments';

const out = '/Users/jarvis/Corsia/projects/corsia/.threads/a8f17a81-7687-4d22-8b07-819464800949/o1-pack/round3-containment';
test('identity remains contained under the Workspace overlay', async ({ page, request }) => {
  test.setTimeout(180_000);
  mkdirSync(out, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await setDebugVersionOverlay(request, false);
  await seed(page, request);
  for (const width of [1080, 1100, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(2000);
    const info = await page.locator('.document-header').evaluate(header => {
      const parent = header.querySelector<HTMLElement>('.metadata-owner')!;
      const button = parent.querySelector<HTMLElement>('button.identity')!;
      const tools = header.querySelector<HTMLElement>('.document-tools')!;
      return { parent: parent.getBoundingClientRect().toJSON(), button: button.getBoundingClientRect().toJSON(), overflow: getComputedStyle(button).overflow, tools: tools.getBoundingClientRect().toJSON(), header: header.getBoundingClientRect().toJSON() };
    });
    console.log('CONTAINMENT', width, JSON.stringify(info));
    await page.screenshot({ path: `${out}/${width}.png`, animations: 'disabled' });
    expect(info.button.left).toBeGreaterThanOrEqual(info.parent.left - 1);
    expect(info.button.right).toBeLessThanOrEqual(info.parent.right + 1);
    expect(info.overflow).toBe('hidden');
    expect(info.button.top).toBeGreaterThanOrEqual(info.tools.bottom - 1);
  }
});
