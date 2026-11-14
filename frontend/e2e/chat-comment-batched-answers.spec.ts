import { expect, test, type Page } from './support/test';
import { chatComposer, createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, releaseMockGate, seedMockResponses } from './support/mock';
import { closeCommentThread, commentEditor, commentEditorClosed, openCommentThread, postedComments } from './support/chatComments';
import { setFocusMode } from './support/focusMode';

const BACKEND = `http://127.0.0.1:${Number(process.env.CORSIA_E2E_BACKEND_PORT ?? 3110)}`;
const text = 'First selected phrase. Second selected phrase.';
const quotes = ['First selected phrase', 'Second selected phrase'];
const lead = 'Untagged lead.';
test.use({ video: 'off', colorScheme: 'light', seedPerTest: true });
test.describe.configure({ timeout: 120_000 });

// Focus is a persisted server setting; a spec that turns it off restores it here, which runs on failure too.
test.afterEach(async ({ request }) => {
  await setFocusMode(request, true);
});

const narrow = (page: Page) => (page.viewportSize()?.width ?? 0) <= 768;

async function capture(page: Page, stage: string) {
  const directory = process.env.CORSIA_CHAT_ANSWERS_CAPTURE_DIR;
  if (directory) await page.screenshot({ path: `${directory}/${stage}-${page.viewportSize()?.width}.png`, animations: 'disabled' });
}

/** Scroll the transcript to its end, where the last row sits above the composer; returns where the row and composer are. */
async function lastRowClear(page: Page) {
  return page.locator('.messages [data-message-id]').last().evaluate(async (node) => {
    // The transcript keeps a composer-high gap under its last row once scrolled to its end.
    let scroller = node.parentElement;
    while (scroller && !(scroller.scrollHeight > scroller.clientHeight && /auto|scroll/.test(getComputedStyle(scroller).overflowY))) scroller = scroller.parentElement;
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
    await new Promise((resolve) => setTimeout(resolve, 150));
    const floater = document.querySelector('.input-floater')?.getBoundingClientRect();
    const box = node.getBoundingClientRect();
    return { bottom: Math.round(box.bottom), composerTop: floater ? Math.round(floater.top) : null };
  });
}

async function expectLastRowClear(page: Page) {
  const { bottom, composerTop } = await lastRowClear(page);
  expect(bottom).toBeLessThanOrEqual(composerTop ?? Infinity);
}

/** Scroll the commented message to the middle of the transcript so its highlight shows, then capture. */
async function captureHighlight(page: Page, row: any, stage: string) {
  await row.evaluate((node: HTMLElement) => node.scrollIntoView({ block: 'center' }));
  await page.waitForTimeout(150);
  await capture(page, stage);
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
  // Posted from its own box while the first Turn runs, so the next Turn gets every comment as one batch.
  await page.getByRole('button', { name: 'Comment', exact: true }).click();
  await commentEditorClosed(page);
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

/**
 * Open a thread whose first Turn is still running, with `count` comments queued behind it; returns their cids.
 * Focus is on, the default, where only a message sent for the person is shown and so answers a comment; `focus: false`
 * shows every assistant step, so closing text answers too.
 */
async function queueComments(page: Page, request: any, title: string, count: number, { focus = true } = {}) {
  if (narrow(page)) await page.setViewportSize({ width: 393, height: 852 });
  if (!focus) await setFocusMode(request, false);
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
  // Ensure the first response is held before a later seed replaces the mock queue.
  await expect.poll(async () => (await (await request.get(`${BACKEND}/api/test/mock-responses`)).json()).call_count).toBe(1);
  for (let index = 0; index < count; index++) await comment(page, target, quotes[index], `Note ${index + 1}`);
  let cids: string[] = [];
  await expect.poll(async () => {
    cids = postedComments(await messages(request, thread)).map(c => c.cid);
    return cids.length;
  }).toBe(count);
  // The posted comments add no chat message: the typed "Start" is the transcript's only user row.
  await expect(page.locator('.messages .message.user')).toHaveCount(1);
  return { thread, row, cids };
}

async function answerReadsInItsThread(page: Page, row: any, quote: string, answer: string, how: { tap?: boolean } = {}) {
  const surface = await openCommentThread(row, page, quote, how);
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
    const rows = await messages(request, thread);
    const placed = rows.flatMap((m: any) => m.chat_comments ?? []).flatMap((c: any) => c.answers ?? [])
      .find((a: any) => a.author_kind === 'agent' && a.text === answers[0]);
    updateId = placed?.assistant_message_id ?? '';
    // The server never sends a step that draws nothing: the update is in no page read.
    return Boolean(updateId) && !rows.some((m: any) => m.id === updateId);
  }, { timeout: 30_000 }).toBe(true);
  // While the Turn still runs, the update adds no chat row and each answer reads in its own thread.
  await expect(page.locator(`[data-message-id="${updateId}"]`)).toHaveCount(0);
  expect(await chatText(page)).not.toContain(answers[0]);
  if (!narrow(page)) await expect(sidebarThread(page, `Batched comments ${count}`)).not.toContainText(answers[0]);
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

for (const width of [1440, 393]) test.describe(`answer sent and repeated in the closing text at ${width}`, () => {
test.use({ viewport: { width, height: width === 393 ? 852 : 1000 }, hasTouch: width === 393, isMobile: width === 393 });
test(`a post_update answer repeated in the closing text reads once in its thread (${width})`, async ({ page, request }) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
  test.skip(test.info().project.name !== 'chromium-desktop', 'Sets its own widths');
  const { thread, row, cids } = await queueComments(page, request, 'Repeated answer', 1);
  const answer = 'Moved it to the next line.';
  // The agent sends the answer, then repeats it in its closing text: only the sent words answer the comment.
  await seedMockResponses(request, [
    { content: '', tool_calls: [{ id: 'update-1', name: 'post_update', arguments: JSON.stringify({ text: `[${cids[0]}] ${answer}` }) }], finish_reason: 'tool_calls' },
    { content: `[${cids[0]}] ${answer}` },
  ]);
  // The closing text is the one row a page read returns: the sent update draws nothing there.
  await expect.poll(async () => (await messages(request, thread)).filter(m => m.role === 'assistant' && m.content === `[${cids[0]}] ${answer}`).length, { timeout: 30_000 }).toBe(1);
  await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0);
  const placed = (await messages(request, thread)).flatMap((m: any) => m.chat_comments ?? []).flatMap((c: any) => c.answers ?? [])
    .filter((a: any) => a.author_kind === 'agent' && a.text === answer);
  expect(placed).toHaveLength(1);
  const surface = await answerReadsInItsThread(page, row, quotes[0], answer, { tap: width === 393 });
  await expect(surface.getByText(answer)).toHaveCount(1);
  await expect(surface).toContainText('1 reply');
  await expect(surface).not.toContainText('2 replies');
  await capture(page, 'repeated-answer-thread');
  await closeCommentThread(page, surface);
  // Chat keeps the closing words inside its folded work, not as a reply of its own.
  expect(await chatText(page)).not.toContain(answer);
});
});

test('an update that answers no comment, beside tagged closing text, places no answer and repeats nothing in chat', async ({ page, request }) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
  const { thread, cids } = await queueComments(page, request, 'Untagged update', 1);
  const answer = 'Friday is skipped.';
  // The Turn sends an update that answers no comment, then closes with the tagged answer the person is never sent.
  // The e2e app turns the Focus nudge off (its unit tests own it), so this shows what the person sees until a nudge.
  await seedMockResponses(request, [
    { content: '', tool_calls: [{ id: 'update-1', name: 'post_update', arguments: JSON.stringify({ text: 'Moved the meeting.' }) }], finish_reason: 'tool_calls' },
    { content: `[${cids[0]}] ${answer}` },
  ]);
  await expect.poll(async () => (await messages(request, thread)).some(m => m.role === 'assistant' && m.content === `[${cids[0]}] ${answer}`), { timeout: 30_000 }).toBe(true);
  await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0);
  const rows = await messages(request, thread);
  expect(rows.flatMap((m: any) => m.chat_comments ?? []).flatMap((c: any) => c.answers ?? []).filter((a: any) => a.author_kind === 'agent')).toHaveLength(0);
  await expect(page.locator('.messages').getByText('Moved the meeting.')).toBeVisible();
  // Nothing was placed, so the tagged closing text stays whole in the folded work, never in a comment thread.
  await expandWork(page);
  expect((await chatText(page)).split(answer).length - 1).toBe(1);
  await capture(page, 'untagged-update-unanswered');
});

test('an answer written into the reply never streams into chat (Focus off)', async ({ page, request }) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
  const { thread, row, cids } = await queueComments(page, request, 'Streamed answer', 1, { focus: false });
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

for (const width of [1440, 900, 393]) test.describe(`answers-only turn at ${width}`, () => {
test.use({ viewport: { width, height: width === 393 ? 852 : 1000 }, hasTouch: width === 393, isMobile: width === 393 });
test(`a turn whose only sent words are answers adds nothing to chat, live and after a reload (${width})`, async ({ page, request }) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
  test.skip(test.info().project.name !== 'chromium-desktop', 'Sets its own widths');
  const { thread, row, cids } = await queueComments(page, request, 'Answers only turn', 1);
  // The highlight while no answer has come, to read beside the one an unread answer leaves.
  await captureHighlight(page, row, 'highlight-no-answer');
  const answer = 'Only an answer.';
  await seedMockResponses(request, [
    { content: '', tool_calls: [{ id: 'update-1', name: 'post_update', arguments: JSON.stringify({ text: `[${cids[0]}] ${answer}` }) }], finish_reason: 'tool_calls' },
    { content: 'Done.', completion_gate: 'answers-only' },
  ]);
  const blocks = () => page.locator('.messages [data-work-block]');
  const titled = (title: RegExp) => blocks().filter({ has: page.locator('.fold-toggle', { hasText: title }) });
  await expect(page.getByRole('button', { name: 'Stop turn' })).toBeVisible();
  // While the turn runs, its answers-only step adds one working line and nothing else.
  await expect(titled(/Working/)).toHaveCount(1);
  await expect(page.locator('.messages [data-work-block].live-work')).toHaveCount(1);
  await releaseMockGate(request, 'answers-only');
  let stepId = '';
  await expect.poll(async () => {
    const rows = await messages(request, thread);
    stepId = rows.flatMap((m: any) => m.chat_comments ?? []).flatMap((c: any) => c.answers ?? [])
      .find((a: any) => a.author_kind === 'agent' && a.text === answer)?.assistant_message_id ?? '';
    return Boolean(stepId) && !rows.some((m: any) => m.id === stepId);
  }, { timeout: 30_000 }).toBe(true);
  await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0);
  await captureHighlight(page, row, 'highlight-unread-answer');
  // Once it ends no line is left for it: the one folded line counts the steps it lists when opened (#2602).
  await expect(page.locator('.messages [data-work-block].live-work')).toHaveCount(0);
  const labels = await blocks().evaluateAll((nodes) => nodes.map((node) => (node.querySelector('.fold-toggle') as HTMLElement).innerText));
  expect(labels.map((label) => label.replace(/ · under 1 min$/, ''))).toEqual(['Context updated', 'Worked · 3 steps']);
  // What chat draws: the same rows and the same fold labels live and after a reload, none of them the answer.
  const drawn = async () => ({
    ids: await page.locator('.messages [data-message-id]').evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-message-id'))),
    folds: await page.locator('.messages .fold-toggle').allInnerTexts(),
  });
  await expandWork(page);
  const live = await drawn();
  // Opened, the fold lists the first Turn's words, the sending tool call and the closing words; the answer is none of them.
  const openedRows = await titled(/Worked/).locator('[data-message-id]').allInnerTexts();
  expect(openedRows.map((row) => row.split('\n')[0])).toEqual(['Working on it', 'Used a tool', 'Done.']);
  expect(live.ids).not.toContain(stepId);
  expect(await chatText(page)).not.toContain(answer);
  await expectLastRowClear(page);
  await capture(page, 'answers-only-live');
  await page.reload();
  if (narrow(page)) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await sidebarThread(page, 'Answers only turn').click();
  await waitForWsConnected(page);
  await expect(page.locator('.messages [data-message-id]').first()).toBeVisible();
  await expandWork(page);
  await expect(page.locator('.messages [data-message-id]')).toHaveCount(live.ids.length);
  const reloaded = await drawn();
  expect(reloaded.ids).toEqual(live.ids);
  expect(reloaded.folds).toEqual(live.folds);
  await expectLastRowClear(page);
  await capture(page, 'answers-only-reloaded');
  await answerReadsInItsThread(page, row, quotes[0], answer, { tap: width === 393 });
  await capture(page, 'thread-open');
});
});
