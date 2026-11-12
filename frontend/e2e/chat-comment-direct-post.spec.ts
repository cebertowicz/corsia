import { expect, test } from './support/test';
import { chatComposer, createSession, openAppPastLanding, sendButton, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, seedMockResponses } from './support/mock';
import { commentEditor, commentOnMessage, openCommentThread } from './support/chatComments';

test.use({ seedPerTest: true, viewport: { width: 1440, height: 900 } });
test('posts from the comment box and keeps answers out of chat', async ({ page, request }) => {
  test.setTimeout(120_000);
  test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
  const thread = await createSession(request, 'Direct comment post');
  const id = await injectMessage(request, thread, { role: 'assistant', content: 'Selected phrase for a comment.', metadata: { for_user: true } }, TEST_TOKEN);
  await seedMockResponses(request, [
    { content: '', tool_calls: [{ id: 'update-1', name: 'post_update', arguments: JSON.stringify({ text: 'Text for the chat.\n\n[c2.1] Answer in the comment.' }) }], finish_reason: 'tool_calls' },
    { content: 'Finished working.' },
  ]);
  await openAppPastLanding(page);
  await sidebarThread(page, 'Direct comment post').click();
  await waitForWsConnected(page);
  const row = page.locator(`[data-message-id="${id}"]`);
  await expect(row).toBeVisible();
  await page.evaluate((messageId) => {
    const body = document.querySelector<HTMLElement>(`[data-message-id="${messageId}"] .long-message-body`)!;
    const quote = 'Selected phrase';
    const start = body.textContent!.indexOf(quote);
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    let position = 0, found = false;
    for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
      if (!found && start < position + node.length) { range.setStart(node, start - position); found = true; }
      if (found && start + quote.length <= position + node.length) { range.setEnd(node, start + quote.length - position); break; }
      position += node.length;
    }
    const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
  }, id);
  await page.locator('.sel-comment').click();
  await commentEditor(page).fill('My direct comment');
  await page.getByRole('dialog', { name: 'New comment' }).getByRole('button', { name: 'Comment', exact: true }).click();
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  let cid = '';
  await expect.poll(async () => {
    const response = await request.get(`/api/threads/${thread}/messages?token=${encodeURIComponent(TEST_TOKEN)}`);
    const body = await response.json();
    cid = body.messages.find((message: any) => message.metadata?.reply_context?.comments?.length)?.metadata.reply_context.comments[0].cid ?? '';
    return cid;
  }).not.toBe('');
  expect(cid).toBe('c2.1');
  await commentOnMessage(row, 'Selected phrase');
  await expect(commentEditor(page)).toHaveCount(0);
  const comment = await openCommentThread(row, page, 'Selected phrase');
  await expect(comment).toContainText('My direct comment');
  await expect(comment).toContainText('Answer in the comment.', { timeout: 30000 });
  await expect(page.locator('.messages')).not.toContainText('Answer in the comment.');
  await expect(page.locator('.messages')).toContainText('Text for the chat.');
  await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0);
  await seedMockResponses(request, [{ content: 'Reply received.' }]);
  await comment.getByPlaceholder('Reply…').fill('My direct reply');
  await comment.getByRole('button', { name: 'Send reply' }).click();
  await expect(comment).toContainText('My direct reply');
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0);
  await chatComposer(page).fill('A plain follow-up');
  await seedMockResponses(request, [{ content: 'Follow-up received.' }]);
  // Send, not Enter: on a touch device Enter starts a new line.
  await sendButton(page).click();
  const bubble = page.locator('.messages .message.user').filter({ hasText: 'A plain follow-up' });
  await expect(bubble).toBeVisible();
  await expect(bubble.locator('.sent-comment')).toHaveCount(0);
});
