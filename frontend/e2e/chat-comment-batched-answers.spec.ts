import { expect, test, type Page } from './support/test';
import { chatComposer, createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, seedMockResponses } from './support/mock';
import { closeCommentThread, commentEditor, commentEditorClosed, openCommentThread } from './support/chatComments';

const BACKEND = `http://127.0.0.1:${Number(process.env.CORSIA_E2E_BACKEND_PORT ?? 3110)}`;
const text = 'First selected phrase. Second selected phrase.';
const quotes = ['First selected phrase', 'Second selected phrase'];
const lead = 'Untagged lead.';
test.use({ video: 'off', colorScheme: 'light', seedPerTest: true });
test.describe.configure({ timeout: 120_000 });

const narrow = (page: Page) => (page.viewportSize()?.width ?? 0) <= 768;

async function capture(page: Page, stage: string) {
  const directory = process.env.CORSIA_CHAT_ANSWERS_CAPTURE_DIR;
  if (directory) await page.screenshot({ path: `${directory}/${stage}-${page.viewportSize()?.width}.png`, animations: 'disabled' });
}

async function comment(page: Page, id: string, quote: string, note: string) {
  await page.evaluate(({ id, quote }) => {
    const body = document.querySelector<HTMLElement>(`[data-message-id="${id}"] .long-message-body`)!;
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
  }, { id, quote });
  await page.locator('.sel-comment').click();
  await commentEditor(page).fill(note);
  await page.getByRole('button', { name: 'Comment', exact: true }).click();
  await commentEditorClosed(page);
  // Sent while the first Turn runs, so the next Turn gets every comment as one batch.
  await page.getByRole('button', { name: 'Send message' }).click();
}

async function messages(request: any, thread: string) {
  const response = await request.get(`/api/threads/${thread}/messages?token=${encodeURIComponent(TEST_TOKEN)}`);
  return (await response.json()).messages as any[];
}

/** The chat transcript's own rows, without the comment cards beside them. */
async function chatText(page: Page) {
  return (await page.locator('.messages .message').allInnerTexts()).join('\n');
}

/** Open every collapsed block of working steps, where a reply's own words show while it is written and after. */
async function expandWork(page: Page) {
  const collapsed = page.locator('.messages .fold-toggle[aria-expanded="false"]');
  for (let left = 10; left > 0 && await collapsed.count(); left--) await collapsed.first().click();
}

/** Open a thread whose first Turn is still running, with `count` comments queued behind it; returns their cids. */
async function queueComments(page: Page, request: any, title: string, count: number) {
  if (narrow(page)) await page.setViewportSize({ width: 393, height: 852 });
  await seedMockResponses(request, [{ content: 'Working on it', completion_gate: 'first-turn' }]);
  const thread = await createSession(request, title);
  // Earlier rows keep the commented message clear of the header on a phone.
  await injectMessage(request, thread, { role: 'assistant', content: 'Earlier context.\n\n'.repeat(20), metadata: { for_user: true } }, TEST_TOKEN);
  const target = await injectMessage(request, thread, { role: 'assistant', content: text, metadata: { for_user: true } }, TEST_TOKEN);
  await openAppPastLanding(page);
  await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
  if (narrow(page)) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await sidebarThread(page, title).click();
  await waitForWsConnected(page);
  const row = page.locator(`[data-message-id="${target}"]`);
  await expect(row).toBeVisible();
  await chatComposer(page).fill('Start');
  await page.getByRole('button', { name: 'Send message' }).click();
  for (let index = 0; index < count; index++) await comment(page, target, quotes[index], `Note ${index + 1}`);
  let cids: string[] = [];
  await expect.poll(async () => {
    cids = (await messages(request, thread)).filter(m => m.role === 'user' && m.metadata?.reply_context?.comments?.length)
      .flatMap(m => m.metadata.reply_context.comments.map((c: any) => c.cid));
    return cids.length;
  }).toBe(count);
  return { thread, row, cids };
}

async function answerReadsInItsThread(page: Page, row: any, quote: string, answer: string) {
  const surface = await openCommentThread(row, page, quote);
  await expect(surface.getByText(answer)).toBeVisible();
  return surface;
}

for (const count of [1, 2]) test(`${count} queued comment(s): an answer sent mid-turn shows only in its thread`, async ({ page, request }) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
  const { thread, row, cids } = await queueComments(page, request, `Batched comments ${count}`, count);
  const answers = cids.map((_, index) => `Answer ${index + 1}.`);
  const update = cids.map((cid, index) => `[${cid}] ${answers[index]}`).join('\n\n');
  // Seeding again releases the first Turn: the batch goes to the next Turn, which answers with post_update and keeps running.
  await seedMockResponses(request, [
    { content: '', tool_calls: [{ id: 'update-1', name: 'post_update', arguments: JSON.stringify({ text: update }) }], finish_reason: 'tool_calls' },
    { content: 'Still working', completion_gate: 'second-turn' },
  ]);
  let updateId = '';
  await expect.poll(async () => {
    const row = (await messages(request, thread)).find(m => m.role === 'assistant' && m.content === update);
    updateId = row?.id ?? '';
    return row?.chat_visible_content;
  }, { timeout: 30_000 }).toBe('');
  // While the Turn still runs, the update adds no chat row and each answer reads in its own thread.
  await expect(page.locator(`[data-message-id="${updateId}"]`)).toHaveCount(0);
  expect(await chatText(page)).not.toContain(answers[0]);
  await capture(page, `midturn-${count}`);
  for (let index = 0; index < count; index++) {
    const surface = await answerReadsInItsThread(page, row, quotes[index], answers[index]);
    if (index === 0) await capture(page, `midturn-thread-${count}`);
    await closeCommentThread(page, surface);
  }
  expect((await request.post(`${BACKEND}/api/test/mock-gates/second-turn/release`)).ok()).toBeTruthy();
  await expect.poll(async () => (await messages(request, thread)).some(m => m.role === 'assistant' && m.content === 'Still working'), { timeout: 30_000 }).toBe(true);
  await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0);
  await expandWork(page);
  await expect(page.locator('.messages').getByText('Still working')).toBeVisible();
  await expect(page.locator(`[data-message-id="${updateId}"]`)).toHaveCount(0);
  for (const answer of answers) expect(await chatText(page)).not.toContain(answer);
});

test('an answer written into the reply never streams into chat', async ({ page, request }) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
  const { thread, row, cids } = await queueComments(page, request, 'Streamed answer', 1);
  const answer = 'Streamed answer, long enough that the reply pauses partway through it.';
  // The mock streams the first half, which ends inside the answer, then holds until released.
  const reply = `${lead}\n\n[${cids[0]}] ${answer}`;
  await seedMockResponses(request, [{ content: reply, content_gate: 'mid-stream' }]);
  await expect(async () => {
    await expandWork(page);
    await expect(page.locator('.messages').getByText(lead)).toBeVisible({ timeout: 1_000 });
  }).toPass({ timeout: 30_000 });
  const chat = await chatText(page);
  expect(chat).not.toContain(`[${cids[0]}`);
  expect(chat).not.toContain('Streamed answer');
  await page.locator('.messages').getByText(lead).evaluate((node) => node.scrollIntoView({ block: 'center' }));
  await capture(page, 'midstream');
  expect((await request.post(`${BACKEND}/api/test/mock-gates/mid-stream/release`)).ok()).toBeTruthy();
  // Once written, the reply keeps its untagged words in chat and the answer reads in its thread.
  await expect.poll(async () => (await messages(request, thread)).find(m => m.role === 'assistant' && m.content === reply)?.chat_visible_content, { timeout: 30_000 }).toBe(lead);
  await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0);
  await expandWork(page);
  await expect(page.locator('.messages .message').filter({ hasText: lead })).not.toHaveCount(0);
  await answerReadsInItsThread(page, row, quotes[0], answer);
  expect(await chatText(page)).not.toContain('Streamed answer');
  await capture(page, 'after');
});
