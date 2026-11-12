import { expect, test } from '../support/test';
import { chatComposer, createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage, seedMockResponses } from '../support/mock';
import { setDebugVersionOverlay } from '../support/heldComments';


for (const [width, height, project] of [[1440, 900, 'chromium-desktop'], [900, 900, 'chromium-desktop'], [900, 420, 'chromium-desktop'], [393, 852, 'chromium-mobile'], [393, 350, 'chromium-mobile'], [393, 420, 'chromium-mobile']] as const) for (const theme of ['light', 'dark'] as const) {
 test(`chat comment remains usable at ${width}x${height} ${theme}`, async ({ page, request }, info) => {
  test.skip(info.project.name !== project);
  test.setTimeout(90_000);
  await page.setViewportSize({ width, height: Math.max(height, 852) });
  await setDebugVersionOverlay(request, false);
  const title = `Chat visibility ${width} ${height} ${theme}`;
  const thread = await createSession(request, title);
  await injectMessage(request, thread, { role: 'assistant', content: 'Earlier context.\n\n'.repeat(20), metadata: { for_user: true } }, TEST_TOKEN);
  const id = await injectMessage(request, thread, { role: 'assistant', content: 'A selected phrase for a comment.', metadata: { for_user: true } }, TEST_TOKEN);
  await openAppPastLanding(page);
  await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
  if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await sidebarThread(page, title).click();
  await waitForWsConnected(page);
  const row = page.locator(`[data-message-id="${id}"]`);
  await expect(row).toBeVisible();
  await row.evaluate(el => {
   const walker = document.createTreeWalker(el.querySelector('.long-message-body')!, NodeFilter.SHOW_TEXT);
   let node = walker.nextNode() as Text | null;
   while (node && !node.data.includes('selected phrase')) node = walker.nextNode() as Text | null;
   if (!node) throw new Error('Selected text was not rendered');
   const start = node.data.indexOf('selected phrase');
   const range = document.createRange(); range.setStart(node, start); range.setEnd(node, start + 'selected phrase'.length);
   const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
  });
  await page.locator('.sel-comment').click();
  const input = page.getByLabel('Comment', { exact: true });
  await input.focus();
  await page.setViewportSize({ width, height });
  for (let i = 0; i < 8; i++) {
   await input.pressSequentially(`typed line ${i}`);
   if (i < 7) await input.press('Shift+Enter');
  }
  await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollHeight > field.clientHeight)).toBe(true);
  await page.screenshot({ path: info.outputPath('typed-chat-comment.png'), animations: 'disabled' });

  await input.fill('');
  await page.keyboard.insertText(Array.from({ length: 40 }, (_, i) => `long chat line ${i}`).join('\n'));
  await input.pressSequentially('!');
  await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollHeight > field.clientHeight)).toBe(true);
  const button = page.getByRole('button', { name: 'Comment', exact: true });
  await expect.poll(async () => {
   const field = (await input.boundingBox())!;
   const action = (await button.boundingBox())!;
   const quote = (await page.locator('.composer .quote-line').boundingBox())!;
   const quoteWrap = (await page.locator('.composer .quote-wrap').boundingBox())!;
   return quote.y >= 0 && quote.y >= quoteWrap.y && quote.y + quote.height <= quoteWrap.y + quoteWrap.height && quote.y + quote.height <= field.y && field.y + field.height <= action.y && action.y + action.height <= height;
  }).toBe(true);
  await page.screenshot({ path: info.outputPath('long-chat-comment.png'), animations: 'disabled' });

  await button.click();
  await expect(page.locator('.comment-chip')).toBeVisible();
  await seedMockResponses(request, [{ content: 'Received comment' }]);
  await chatComposer(page).fill('Please address this');
  await page.getByRole('button', { name: 'Send message' }).click();
  await expect(row.locator('.cm-comment-anchor').filter({ hasText: 'selected phrase' })).toBeVisible();

 });
}
