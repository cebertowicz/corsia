import { test } from '../support/test';
import { expect } from '@playwright/test';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { beginComment } from '../support/liveCommentProof';
import { setDebugVersionOverlay } from '../support/heldComments';

const quote = 'target line for comment';
const text = Array.from({ length: 60 }, (_, i) => i === 25 ? quote : `line ${i} with ordinary text`).join('\n\n');

for (const [width, height, project] of [[1440, 900, 'chromium-desktop'], [900, 900, 'chromium-desktop'], [900, 240, 'chromium-desktop'], [393, 852, 'chromium-mobile'], [393, 350, 'chromium-mobile'], [393, 420, 'chromium-mobile']] as const) for (const theme of ['light', 'dark'] as const) {
 test(`comment action remains visible after long input at ${width}x${height} ${theme}`, async ({ page, request }, info) => {
  test.skip(info.project.name !== project);
  test.setTimeout(90_000);
  await page.setViewportSize({ width, height: Math.max(height, 852) });
  await setDebugVersionOverlay(request, false);
  const doc = await seedLiveDocument(request, `send-visible-${width}`, text);
  await openLiveDocument(page, doc, 'line 0 with ordinary text');
  await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
  const scroller = page.locator('.cm-scroller');
  await scroller.evaluate(e => { e.scrollTop = e.scrollHeight * 0.4; });
  await expect(page.locator('.cm-line').filter({ hasText: quote })).toBeVisible();
  await scroller.evaluate(e => {
   const target = [...document.querySelectorAll<HTMLElement>('.cm-line')].find(n => n.textContent?.includes('target line for comment'))!;
   e.scrollTop += target.getBoundingClientRect().top - (e.getBoundingClientRect().top + e.getBoundingClientRect().height * 0.7);
  });
  await beginComment(page, quote);
  const input = page.getByPlaceholder('Comment, or @ to tag an agent');
  await input.focus();
  await page.setViewportSize({ width, height });
  for (let i = 0; i < 8; i++) {
   await input.pressSequentially(`typed line ${i}`);
   if (i < 7) await input.press('Shift+Enter');
  }
  await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollHeight > field.clientHeight)).toBe(true);
  await page.screenshot({ path: info.outputPath(`comment-typed-${width}.png`), animations: 'disabled' });
  await input.fill(Array.from({ length: 40 }, (_, i) => `long comment line ${i}`).join('\n'));
  await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollHeight > field.clientHeight)).toBe(true);
  await input.evaluate((field: HTMLTextAreaElement) => { field.scrollTop = field.scrollHeight; });
  const button = page.getByRole('button', { name: 'Comment', exact: true }).last();
  const card = width === 393 ? page.getByRole('dialog', { name: 'Document comments' }) : page.locator('[data-popover]');
  await page.screenshot({ path: info.outputPath(`comment-before-assert-${width}.png`), animations: 'disabled' });
  await expect.poll(async () => {
   const cardBox = (await card.boundingBox())!;
   const buttonBox = (await button.boundingBox())!;
   const inputBox = (await input.boundingBox())!;
   const quoteBox = (await page.locator('.composer .quote-line').boundingBox())!;
   const quoteWrap = (await page.locator('.composer .quote-wrap').boundingBox())!;
   const insideClips = await button.evaluate(element => {
    const box = element.getBoundingClientRect();
    for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
     if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(ancestor).overflowY)) continue;
     const clip = ancestor.getBoundingClientRect();
     if (box.top < clip.top || box.bottom > clip.bottom) return false;
    }
    return true;
   });
   return insideClips && quoteBox.y >= quoteWrap.y && quoteBox.y + quoteBox.height <= quoteWrap.y + quoteWrap.height && quoteBox.y >= cardBox.y && quoteBox.y + quoteBox.height <= inputBox.y && inputBox.y + inputBox.height <= buttonBox.y && cardBox.y + cardBox.height <= height && buttonBox.y + buttonBox.height <= Math.min(height, cardBox.y + cardBox.height);
  }).toBe(true);
  await page.screenshot({ path: info.outputPath(`comment-long-${width}.png`), animations: 'disabled' });
  await button.click();
  await expect(page.getByText('long comment line 0')).toBeVisible();
 });
}
