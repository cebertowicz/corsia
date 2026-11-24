import { test } from '../support/test';
import { expect } from '@playwright/test';
import { raiseAsk } from '../support/reviewAsk';
import { seed, open } from '../support/reviewFixtures';
import { askFormOf, expectAskPlacement } from '../support/askPlacement';
import { setDebugVersionOverlay } from '../support/heldComments';


for (const [width, height, mode, project] of [[1440, 900, 'default', 'chromium-desktop'], [900, 900, 'default', 'chromium-desktop'], [900, 420, 'default', 'chromium-desktop'], [393, 852, 'phone', 'chromium-mobile'], [393, 420, 'phone', 'chromium-mobile'], [393, 350, 'phone', 'chromium-mobile']] as const) for (const theme of ['light', 'dark'] as const) {
 test(`long review note keeps Send back reachable at ${width}x${height} ${theme}`, async ({ page, request }, info) => {
  test.skip(info.project.name !== project);
  test.setTimeout(90_000);
  await setDebugVersionOverlay(request, false);
  const doc = await seed(request, `Long review note ${width} ${theme}`);
  raiseAsk(doc.threadId, doc.artifactId, 'Is this ready to ship?');
  await open(page, doc, mode, theme);
  await page.setViewportSize({ width, height });
  // A phone's ask is the docked card; on a larger screen it is the row in the document header (its receipt is a `.review-bar` too).
  const form = askFormOf(mode);
  await expectAskPlacement(page, form);
  const bar = page.locator(form === 'docked' ? '[data-ask-card]' : '.review-bar');
  await bar.getByRole('button', { name: 'Send back', exact: true }).click();
  const note = bar.getByRole('textbox');
  for (let i = 0; i < 8; i++) {
   await note.pressSequentially(`typed review line ${i}`);
   if (i < 7) await note.press('Shift+Enter');
  }
  await page.screenshot({ path: info.outputPath('typed-review-note.png'), animations: 'disabled' });

  await note.fill('');
  await page.keyboard.insertText(Array.from({ length: 40 }, (_, i) => `review note line ${i}`).join('\n'));
  await expect.poll(() => note.evaluate((field: HTMLTextAreaElement) => field.scrollHeight > field.clientHeight)).toBe(true);
  await expect.poll(() => note.evaluate((field: HTMLTextAreaElement) => field.clientHeight)).toBeGreaterThan(45);
  await expect.poll(() => note.evaluate((field: HTMLTextAreaElement) => field.scrollHeight - field.clientHeight - field.scrollTop)).toBeLessThanOrEqual(1);
  const send = bar.getByRole('button', { name: 'Send back', exact: true });
  await expect.poll(async () => {
   const box = (await send.boundingBox())!;
   return box.y >= 0 && box.y + box.height <= height && box.x >= 0 && box.x + box.width <= width;
  }).toBe(true);
  await page.screenshot({ path: info.outputPath('long-review-note.png'), animations: 'disabled' });

  await send.click();
  await expect(bar.locator('.rb-receipt')).toContainText('Sent back');

 });
}
