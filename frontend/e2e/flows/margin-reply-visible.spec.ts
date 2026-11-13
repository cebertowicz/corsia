import { test } from '../support/test';
import { expect } from '@playwright/test';
import { SUGGESTION2, card, open } from '../support/marginCards';
import { setDebugVersionOverlay } from '../support/heldComments';
import { expectNoCardScroller, expectReachableByOuterScroll } from '../support/discussionVisible';


// At 900px the document split is too narrow for margin cards; the thread-popover spec covers that viewport.
for (const width of [1440] as const) for (const theme of ['light', 'dark'] as const) {
 test(`long margin reply stays usable near the bottom at ${width} in ${theme}`, async ({ page, request }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  await page.setViewportSize({ width, height: 900 });
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
  const send = wrap.getByRole('button', { name: 'Send reply' });
  // The field grows with its text and never scrolls itself, and nothing in the card scrolls; the caret line and Send
  // stay on screen, and the card never runs past the window's sides.
  const checkOutcome = async () => {
   await expectNoCardScroller(wrap);
   await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollHeight <= field.clientHeight + 1)).toBe(true);
   await expect(send).toBeVisible();
   await expect.poll(async () => {
    const box = (await send.boundingBox())!;
    return box.y >= 0 && box.y + box.height <= 900 && box.x >= 0 && box.x + box.width <= width;
   }).toBe(true);
   await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => {
    const box = field.getBoundingClientRect(), line = Number.parseFloat(getComputedStyle(field).lineHeight) || 20;
    return box.bottom <= innerHeight + 1 && box.bottom - line >= -1;
   })).toBe(true);
   await expect.poll(async () => {
    const box = (await wrap.boundingBox())!;
    return box.x >= 0 && box.x + box.width <= width;
   }).toBe(true);
   await send.click({ trial: true });
  };
  for (let i = 0; i < 40; i++) {
   await input.pressSequentially(`typed reply ${i}`);
   if (i < 39) await input.press('Shift+Enter');
  }
  await checkOutcome();
  await page.screenshot({ path: info.outputPath('typed-margin-reply.png'), animations: 'disabled' });

  await input.fill(Array.from({ length: 40 }, (_, i) => `margin reply line ${i}`).join('\n'));
  await input.pressSequentially('!');
  await checkOutcome();
  await page.screenshot({ path: info.outputPath('long-margin-reply.png'), animations: 'disabled' });

  await page.setViewportSize({ width, height: 700 });
  await expect.poll(async () => {
   const box = (await send.boundingBox())!;
   return box.y >= 0 && box.y + box.height <= 700;
  }).toBe(true);
  await send.click({ trial: true });
  await send.click();
  await expect(wrap).toContainText('margin reply line 0');
  await input.fill(Array.from({ length: 40 }, (_, i) => `second margin reply ${i}`).join('\n'));
  // Scrolling the document reaches the comment and the first reply; nothing inside the card scrolls.
  await expectNoCardScroller(wrap);
  await expectReachableByOuterScroll(wrap, 'Match the intro.');
  await expectReachableByOuterScroll(wrap, 'margin reply line 0');
  await send.click();
  await expect(wrap).toContainText('second margin reply 0');

 });
}
