import { test, expect } from '../support/test';
import { createSession, openAppPastLanding, TEST_TOKEN } from '../support/corsia';
import { injectMessage, seedMockResponses, waitForThreadReply } from '../support/mock';

test.use({ seedPerTest: true });

test('a posted chat comment and delivered answer reach Notifications', async ({ page, request }, info) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1' || info.project.name !== 'chromium-desktop');
  test.setTimeout(120_000);
  const thread = await createSession(request, 'Producer journey');
  await injectMessage(request, thread, { role: 'assistant', content: 'Message being commented on.', metadata: { for_user: true } }, TEST_TOKEN);
  await seedMockResponses(request, [{ content: 'Comment received', match_thread: thread }]);
  const post = await request.post(`/api/threads/${thread}/chat-comments`, {
    headers: { Authorization: `Bearer ${TEST_TOKEN}` },
    data: { input_id: 'producer-comment', comment: { id: 'comment-1', seq: 1, from_off: 1, to_off: 8, quote: 'Message', anchor_prefix: '', anchor_suffix: '', note: 'Producer question' } },
  });
  expect(post.ok()).toBeTruthy();
  const posted = await post.json();
  const cid = posted.messages[0].chat_comments[0].cid;
  await waitForThreadReply(request, thread, 'Comment received');
  await injectMessage(request, thread, { role: 'assistant', content: `[${cid}] Producer answer.`, metadata: { for_user: true } }, TEST_TOKEN);
  await expect.poll(async () => {
    const response = await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } });
    const queue = await response.json();
    return queue.notifications.items.some((item: { key: string }) => item.key === `chat-reply:${thread}`);
  }).toBe(true);
  await openAppPastLanding(page);
  await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
  await page.getByRole('tab', { name: /Notifications/ }).click();
  await page.locator('[data-activity-row]').filter({ hasText: 'Producer journey' }).click();
  await expect(page.getByText('Producer question', { exact: false })).toBeVisible();
  await expect(page.getByText('Producer answer.', { exact: false })).toBeVisible();
});
