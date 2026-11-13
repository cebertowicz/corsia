import fs from 'node:fs';
import { expect, test, type APIRequestContext, type Locator, type Page } from './support/test';
import { chatComposer, createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, seedMockResponses } from './support/mock';
import { closeCommentThread, commentEditor, commentOnMessage, openCommentThread, postedComments } from './support/chatComments';
import { capture, framesDirectory as directory, selectPhrase } from './support/chatCommentFrames';

const backend = `http://127.0.0.1:${Number(process.env.CORSIA_E2E_BACKEND_PORT ?? 3110)}`;
test.describe.configure({ timeout: 180_000 });
test.use({ seedPerTest: true, colorScheme: 'light', video: 'off' });

/** A cid is `c<input order>.<position>` (backend/infra/chat_comment_ids.py), so the newest input's sorts last. */
const byCid = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true });

/** The cid the server gave the newest posted comment, read through the messages API. */
async function newestCid(request: APIRequestContext, thread: string): Promise<string> {
  let cid = '';
  await expect.poll(async () => {
    const body = await (await request.get(`/api/threads/${thread}/messages?token=${encodeURIComponent(TEST_TOKEN)}`)).json();
    cid = postedComments(body.messages).map(comment => comment.cid).sort(byCid).at(-1) ?? '';
    return cid;
  }).not.toBe('');
  return cid;
}

/** Post a comment on `quote` from its own Comment button and wait for the sent highlight. */
async function postComment(page: Page, row: Locator, messageId: string, quote: string, text: string) {
  await selectPhrase(page, messageId, quote);
  await commentEditor(page).fill(text);
  await page.getByRole('dialog', { name: 'New comment' }).getByRole('button', { name: 'Comment', exact: true }).click();
  await commentOnMessage(row, quote);
  await expect(commentEditor(page)).toHaveCount(0);
}

/**
 * Open a comment's thread after leaving the composer, as a person closes the keyboard first: on a
 * phone a focused composer shrinks the transcript's top space, so the header covers the first message.
 */
async function openThread(row: Locator, page: Page, quote: string) {
  const mark = row.locator('.cm-comment-anchor').filter({ hasText: quote });
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await expect.poll(async () => {
    const [markBox, headerBox] = [await mark.boundingBox(), await page.locator('.shell-header').boundingBox()];
    return !!markBox && markBox.y >= (headerBox ? headerBox.y + headerBox.height : 0);
  }).toBe(true);
  return openCommentThread(row, page, quote);
}

async function release(request: APIRequestContext, gate: string) {
  expect((await request.post(`${backend}/api/test/mock-gates/${gate}/release`)).ok()).toBeTruthy();
}

async function turnFinished(page: Page) {
  await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0, { timeout: 30000 });
}

/** Capture the open thread, then close it and capture the transcript it covered. */
async function captureOpenAndClosed(page: Page, card: Locator, chatTarget: () => Locator, name: string) {
  await capture(page, card, `${name}-card`);
  await closeCommentThread(page, card);
  await capture(page, chatTarget(), `${name}-chat`);
}

const MESSAGE = 'Selected phrase for a comment. Second phrase gets chat text. Third phrase rides with typed text.';

for (const width of [1440, 900, 393]) test(`direct comment frames ${width}`, async ({ page, request }) => {
  test.skip(!directory || process.env.CORSIA_E2E_MOCK !== '1' || test.info().project.name !== 'chromium-desktop', 'Requires a disposable mock instance and capture directory');
  fs.mkdirSync(directory!, { recursive: true });
  await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
  const thread = await createSession(request, `Direct frames ${width}`);
  const id = await injectMessage(request, thread, { role: 'assistant', content: MESSAGE, metadata: { for_user: true } }, TEST_TOKEN);
  const messages = page.locator('.messages');
  // Scenario 1, comment-only: the agent answers in the comment thread and writes nothing for the chat.
  await seedMockResponses(request, [{ content: '[c2.1] Answer in the comment.', response_gate: `only-${width}` }]);
  await openAppPastLanding(page);
  if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await sidebarThread(page, `Direct frames ${width}`).click();
  await waitForWsConnected(page);
  const row = page.locator(`[data-message-id="${id}"]`);
  await expect(row).toBeVisible();
  await selectPhrase(page, id, 'Selected phrase');
  await commentEditor(page).fill('My direct comment');
  await capture(page, page.getByRole('dialog', { name: 'New comment' }), `before-post-${width}`);
  await page.getByRole('dialog', { name: 'New comment' }).getByRole('button', { name: 'Comment', exact: true }).click();
  await commentOnMessage(row, 'Selected phrase');
  expect(await newestCid(request, thread)).toBe('c2.1');
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  let card = await openThread(row, page, 'Selected phrase');
  await expect(card).toContainText('My direct comment');
  await captureOpenAndClosed(page, card, () => row, `after-post-${width}`);
  await release(request, `only-${width}`);
  await turnFinished(page);
  card = await openThread(row, page, 'Selected phrase');
  await expect(card).toContainText('Answer in the comment.', { timeout: 30000 });
  await expect(messages).not.toContainText('Answer in the comment.');
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  await captureOpenAndClosed(page, card, () => row, `after-answer-${width}`);
  await expect(messages).not.toContainText('Answer in the comment.');

  // Scenario 2, mixed: the same kind of comment, but the agent also writes text for the chat, which the transcript shows.
  // A cid names the input's admission order, which a fresh instance assigns the same way every run;
  // the seeded answer must name it before posting, and the read after posting proves it.
  const mixedCid = 'c3.1';
  await seedMockResponses(request, [
    { content: '', tool_calls: [{ id: 'update-1', name: 'post_update', arguments: JSON.stringify({ text: `Text for the chat.\n\n[${mixedCid}] Answer next to chat text.` }) }], finish_reason: 'tool_calls', response_gate: `mixed-${width}` },
    { content: 'Finished working.' },
  ]);
  await postComment(page, row, id, 'Second phrase', 'Comment whose answer also writes chat text');
  expect(await newestCid(request, thread)).toBe(mixedCid);
  await release(request, `mixed-${width}`);
  await turnFinished(page);
  card = await openThread(row, page, 'Second phrase');
  await expect(card).toContainText('Answer next to chat text.', { timeout: 30000 });
  await expect(messages).toContainText('Text for the chat.');
  await expect(messages).not.toContainText('Answer next to chat text.');
  await captureOpenAndClosed(page, card, () => messages.getByText('Text for the chat.'), `mixed-answer-${width}`);

  // Scenario 3, comment plus typed text: a comment posted from its box, then text typed and sent the normal way.
  const thirdCid = 'c4.1';
  await seedMockResponses(request, [
    { content: `[${thirdCid}] Answer to the third comment.`, response_gate: `third-${width}` },
    { content: '', tool_calls: [{ id: 'update-2', name: 'post_update', arguments: JSON.stringify({ text: 'Reply to your typed text.' }) }], finish_reason: 'tool_calls' },
    { content: 'Finished working.' },
  ]);
  await postComment(page, row, id, 'Third phrase', 'Comment sent before typed text');
  expect(await newestCid(request, thread)).toBe(thirdCid);
  await chatComposer(page).fill('Text typed after posting a comment');
  await chatComposer(page).press('Enter');
  const bubble = page.locator('.messages .message.user').filter({ hasText: 'Text typed after posting a comment' });
  await expect(bubble).toBeVisible();
  await release(request, `third-${width}`);
  await expect(messages).toContainText('Reply to your typed text.', { timeout: 30000 });
  await turnFinished(page);
  await expect(page.locator('.messages .message.user')).toHaveCount(1);
  await expect(bubble.locator('.sent-comment')).toHaveCount(0);
  await expect(bubble).not.toContainText('Comment sent before typed text');
  card = await openThread(row, page, 'Third phrase');
  await expect(card).toContainText('Answer to the third comment.', { timeout: 30000 });
  await expect(messages).not.toContainText('Answer to the third comment.');
  await captureOpenAndClosed(page, card, () => bubble, `comment-plus-text-${width}`);
});
