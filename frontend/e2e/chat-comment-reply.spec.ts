import { expect, test } from './support/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, seedMockResponses } from './support/mock';
import { closeCommentThread, commentEditor, commentEditorClosed, commentOnMessage, openCommentThread } from './support/chatComments';

test('post a comment from its box, then reply from the thread its highlight opens', async ({ page, request }) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
  test.setTimeout(120_000);
  if ((page.viewportSize()?.width ?? 0) <= 768) await page.setViewportSize({ width: 393, height: 852 });
  const narrow = (page.viewportSize()?.width ?? 0) <= 768;
  const quote = 'selected phrase';
  const capture = async (stage: string) => {
    const directory = process.env.CORSIA_REPLY_CAPTURE_DIR;
    if (!directory) return;
    const width = page.viewportSize()?.width ?? 0;
    const originalTheme = await page.evaluate(() => document.documentElement.dataset.theme);
    for (const theme of ['light', 'dark']) {
      await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
      await page.screenshot({ path: `${directory}/${stage}-${width}-${theme}.png`, animations: 'disabled' });
    }
    await page.evaluate((value) => { if (value) document.documentElement.dataset.theme = value; }, originalTheme);
  };
  const title = 'Reply journey';
  const thread = await createSession(request, title);
  await injectMessage(request, thread, { role: 'assistant', content: 'Earlier context.\n\n'.repeat(20), metadata: { for_user: true } }, TEST_TOKEN);
  const id = await injectMessage(request, thread, { role: 'assistant', content: 'A selected phrase for a comment.', metadata: { for_user: true } }, TEST_TOKEN);
  await openAppPastLanding(page);
  await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
  if (narrow) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await sidebarThread(page, title).click();
  await waitForWsConnected(page);
  const row = page.locator(`[data-message-id="${id}"]`);
  await expect(row).toBeVisible();
  await row.evaluate((el) => {
    const body = el.querySelector('.long-message-body')!;
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode() as Text | null;
    while (node && !node.data.includes('selected phrase')) node = walker.nextNode() as Text | null;
    if (!node) throw new Error('Selected text was not rendered');
    const start = node.data.indexOf('selected phrase');
    const range = document.createRange(); range.setStart(node, start); range.setEnd(node, start + 'selected phrase'.length);
    const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
  });
  await page.locator('.sel-comment').click();
  await commentEditor(page).fill('First comment');
  await capture('compose-comment');
  // The comment posts from its own box: it is never staged in the chat input and never becomes a chat message.
  await seedMockResponses(request, [{ content: 'Received comment' }]);
  await page.getByRole('button', { name: 'Comment', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'New comment' })).toBeHidden();
  await commentEditorClosed(page);
  await expect(page.locator('.comment-chip')).toHaveCount(0);
  // The thread never opens by itself and adds no rows under the message: the comment stays on its highlight.
  await expect(row.locator('.chat-comment-thread')).toHaveCount(0);
  await commentOnMessage(row, quote);
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  const surface = await openCommentThread(row, page, quote);
  await expect(surface.getByText('First comment')).toBeVisible();
  await capture('thread');
  // Replying from the open thread, on both the popover and the phone drawer.
  const replyField = surface.getByPlaceholder('Reply…');
  await expect(replyField).toBeVisible();
  await replyField.fill('Draft reply');
  await capture('compose');
  await seedMockResponses(request, [{ content: 'Received reply' }]);
  await surface.getByRole('button', { name: 'Send reply' }).click();
  // The reply reads in the thread, and a reply send adds no user bubble of its own.
  await expect(surface.getByText('Draft reply')).toBeVisible();
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  await closeCommentThread(page, surface);
  await capture('sent');
  await page.reload();
  if (narrow) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await sidebarThread(page, title).click();
  const reopened = await openCommentThread(row, page, quote);
  await expect(reopened.getByText('First comment')).toBeVisible();
  await expect(reopened.getByText('Draft reply')).toBeVisible();
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
});
