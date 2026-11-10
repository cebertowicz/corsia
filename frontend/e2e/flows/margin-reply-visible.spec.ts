import { test } from '../support/test';
import { expect } from '@playwright/test';
import { SUGGESTION2, card, open } from '../support/marginCards';
import { setDebugVersionOverlay } from '../support/heldComments';

for (const theme of ['light', 'dark'] as const) {
 test(`long margin reply stays usable near the bottom in ${theme}`, async ({ page, request }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  await page.setViewportSize({ width: 1440, height: 900 });
  await setDebugVersionOverlay(request, false);
  const doc = await open(page, request);
  await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
  await page.locator('.cm-line', { hasText: 'Paragraph 8 of' }).first().evaluate(line => {
   const scroller = document.querySelector('.cm-scroller')!;
   scroller.scrollTop += line.getBoundingClientRect().top - (window.innerHeight - 140);
  });
  const wrap = card(page, doc, SUGGESTION2);
  await wrap.locator('.ct-head').click({ position: { x: 14, y: 10 } });
  const input = wrap.locator('textarea');
  for (let i = 0; i < 8; i++) {
   await input.pressSequentially(`typed reply ${i}`);
   if (i < 7) await input.press('Shift+Enter');
  }
  await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollHeight > field.clientHeight)).toBe(true);
  await page.screenshot({ path: info.outputPath('typed-margin-reply.png'), animations: 'disabled' });
  await input.fill(Array.from({ length: 40 }, (_, i) => `margin reply line ${i}`).join('\n'));
  await input.pressSequentially('!');
  await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollHeight > field.clientHeight)).toBe(true);
  const send = wrap.getByRole('button', { name: 'Send reply' });
  await expect.poll(async () => {
   const box = (await send.boundingBox())!;
   const clip = (await page.locator('.drawer-comments.margin-clip').boundingBox())!;
   return box.y >= clip.y && box.y + box.height <= Math.min(900, clip.y + clip.height);
  }).toBe(true);
  await page.screenshot({ path: info.outputPath('long-margin-reply.png'), animations: 'disabled' });
  await send.click();
  await expect(wrap).toContainText('margin reply line 0');
 });
}
