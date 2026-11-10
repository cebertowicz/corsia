import { test } from '../support/test';
import { expect } from '@playwright/test';
import { agentComment, reviewerSession } from '../support/liveConcurrency';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { setDebugVersionOverlay } from '../support/heldComments';

const quote = 'target line for a reply';
const text = Array.from({ length: 45 }, (_, i) => i === 25 ? quote : `document line ${i}`).join('\n\n');
for (const [width, height, project] of [[900, 900, 'chromium-desktop'], [393, 852, 'chromium-mobile']] as const) for (const theme of ['light', 'dark'] as const) {
 test(`long thread reply stays usable at ${width} ${theme}`, async ({ page, request }, info) => {
  test.skip(info.project.name !== project);
  await page.setViewportSize({ width, height });
  await setDebugVersionOverlay(request, false);
  const doc = await seedLiveDocument(request, `reply-visible-${width}-${theme}`, text);
  const reviewer = await reviewerSession(request, `${width}-${theme}`);
  await agentComment(request, doc, reviewer.id, quote, 'Existing thread');
  await openLiveDocument(page, doc, 'document line 0');
  await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
  const scroller = page.locator('.cm-scroller');
  await scroller.evaluate(e => { e.scrollTop = e.scrollHeight * 0.4; });
  await expect(page.locator('.cm-line').filter({ hasText: quote })).toBeVisible();
  await scroller.evaluate(e => {
   const target = [...document.querySelectorAll<HTMLElement>('.cm-line')].find(n => n.textContent?.includes('target line for a reply'))!;
   e.scrollTop += target.getBoundingClientRect().top - (e.getBoundingClientRect().bottom - 120);
  });
  await page.locator('.cm-comment-anchor').filter({ hasText: quote }).click();
  const input = page.getByPlaceholder('Reply…');
  for (let i = 0; i < 8; i++) {
   await input.pressSequentially(`typed reply ${i}`);
   if (i < 7) await input.press('Shift+Enter');
  }
  await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollHeight > field.clientHeight)).toBe(true);
  await page.screenshot({ path: info.outputPath('typed-reply.png'), animations: 'disabled' });
  await input.fill(Array.from({ length: 40 }, (_, i) => `long reply line ${i}`).join('\n'));
  const send = page.getByRole('button', { name: 'Send reply' });
  await expect.poll(async () => {
   const box = (await send.boundingBox())!;
   return box.y >= 0 && box.y + box.height <= height && box.x >= 0 && box.x + box.width <= width;
  }).toBe(true);
  await page.screenshot({ path: info.outputPath('long-reply.png'), animations: 'disabled' });
  await send.click();
  await expect(page.getByText('long reply line 0')).toBeVisible();
 });
}
