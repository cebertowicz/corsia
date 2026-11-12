import fs from 'node:fs';
import { expect, test, type APIRequestContext, type Locator, type Page } from './support/test';
import { createSession, openAppPastLanding, sendChatMessage, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { failQueuedInput, injectMessage, readMockCallCount, seedMockResponses } from './support/mock';
import { commentEditor, commentOnMessage, openCommentThread, postedComments } from './support/chatComments';
import { capture, framesDirectory, selectPhrase } from './support/chatCommentFrames';

/**
 * A comment's post failing, seen where the person wrote it. Refused before it is stored: the box keeps the text and
 * says why, and its one Retry posts it (the refusal is the route's own error answer, routed onto that one post).
 * Stored and then ended: the box closes, and the comment's own thread, opened from its highlight, shows chat's label
 * for that state and chat's Retry for it. A failed comment is a queued input the test ends through the server's own
 * writer for an undeliverable input; an uncertain one is real, stopped after a Turn took it and began its model call.
 * Retrying either posts the comment again through the comment route and never reaches the transcript.
 */
const backend = `http://127.0.0.1:${Number(process.env.CORSIA_E2E_BACKEND_PORT ?? 3110)}`;
test.describe.configure({ timeout: 180_000 });
test.use({ seedPerTest: true, colorScheme: 'light', video: 'off' });

const MESSAGE = 'First phrase is refused once. Second phrase fails after it is stored.';
// The server's own words when it refuses a send because the chat's model is down.
const REFUSAL = 'Model unavailable: the provider is not responding. Please select a different model.';

const UNDELIVERED = 'The accepted input did not reach a Turn.';

/** Answer the comment post carrying `note()` with the route's refusal, while `note()` names one. */
async function refuse(page: Page, note: () => string | null) {
  await page.route(/\/api\/threads\/[^/]+\/chat-comments$/, async route => {
    const now = note();
    if (now && (route.request().postData() ?? '').includes(now)) await route.fulfill({ status: 503, json: { detail: REFUSAL } });
    else await route.fallback();
  });
}

/** The comments the person posted with this note, as the server's page read carries them. */
async function posted(request: APIRequestContext, thread: string, note: string) {
  const response = await request.get(`${backend}/api/threads/${thread}/messages`, { params: { token: TEST_TOKEN } });
  expect(response.ok()).toBeTruthy();
  return postedComments((await response.json()).messages).filter(comment => comment.note === note);
}

/** Press a comment's Retry twice in quick succession, and wait for the comment route's answer. */
async function retryTwice(page: Page, retry: Locator) {
  const retried = page.waitForResponse(response => response.request().method() === 'POST' && /\/chat-comments$/.test(new URL(response.url()).pathname));
  await retry.dblclick();
  const response = await retried;
  expect(response.ok()).toBeTruthy();
  await response.finished();
  await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
}

/** The comment was posted exactly once more: one comment with this note, carried by an input other than `before`. */
async function postedOnceMore(request: APIRequestContext, thread: string, note: string, before: string) {
  await expect.poll(async () => (await posted(request, thread, note)).map(comment => comment.user_message_id !== before)).toEqual([true]);
}

/** The passage being commented (its draft highlight, which is not a control) sits below the shell header and above the open sheet. */
async function passageInView(page: Page, messageId: string): Promise<boolean> {
  return page.evaluate(id => {
    const mark = document.querySelector<HTMLElement>(`[data-message-id="${id}"] .cm-comment-pending:not([role="button"])`);
    const header = document.querySelector('.shell-header')?.getBoundingClientRect();
    const sheet = document.querySelector('[data-adaptive-drawer] .drawer-panel')?.getBoundingClientRect();
    if (!mark || !header || !sheet) return false;
    const rect = mark.getBoundingClientRect();
    return rect.top >= header.bottom && rect.bottom <= sheet.top;
  }, messageId);
}

/** Make the page's primary pointer a finger (Chromium), so `(pointer: coarse)` styles apply from here on. */
async function touch(page: Page) {
  await (await page.context().newCDPSession(page)).send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 });
  await expect.poll(() => page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true);
}

async function release(request: APIRequestContext, gate: string) {
  expect((await request.post(`${backend}/api/test/mock-gates/${gate}/release`)).ok()).toBeTruthy();
}

for (const width of [1440, 393]) test(`a failed comment post keeps its text and retries once, ${width}`, async ({ page, request }) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1' || test.info().project.name !== 'chromium-desktop', 'Requires a disposable mock instance');
  if (framesDirectory) fs.mkdirSync(framesDirectory, { recursive: true });
  const frame = async (target: Parameters<typeof capture>[1], name: string) => { if (framesDirectory) await capture(page, target, `${name}-${width}`); };
  await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
  let refused: string | null = null;
  await refuse(page, () => refused);
  const title = `Comment failure ${width}`;
  const thread = await createSession(request, title);
  const id = await injectMessage(request, thread, { role: 'assistant', content: MESSAGE, metadata: { for_user: true } }, TEST_TOKEN);
  // The retried comment's turn is held open, so the second comment is queued behind it.
  await seedMockResponses(request, [{ content: 'Holding the turn.', response_gate: `held-${width}` }, { content: 'Finished.' }]);
  await openAppPastLanding(page);
  if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await sidebarThread(page, title).click();
  await waitForWsConnected(page);
  const row = page.locator(`[data-message-id="${id}"]`);
  await expect(row).toBeVisible();
  const box = page.getByRole('dialog', { name: 'New comment' });

  // Refused before it is stored: the box stays, with the text and the server's reason, and one Retry.
  refused = 'Refused once';
  await selectPhrase(page, id, 'First phrase', false);
  // Its layout height: the pill scales in as it appears, so its drawn box is briefly smaller.
  if (width === 393) expect(await page.locator('.sel-comment').evaluate(element => (element as HTMLElement).offsetHeight)).toBeGreaterThanOrEqual(44);
  await page.locator('.sel-comment').click();
  await commentEditor(page).fill('Refused once');
  if (width === 393) {
    // A phone's buttons are full touch targets, a target apart, and the passage stays in view between the header and the sheet.
    const [cancel, post] = [await box.getByRole('button', { name: 'Cancel' }).boundingBox(), await box.getByRole('button', { name: 'Comment', exact: true }).boundingBox()];
    expect(cancel!.height).toBeGreaterThanOrEqual(44);
    expect(post!.height).toBeGreaterThanOrEqual(44);
    expect(post!.x - (cancel!.x + cancel!.width)).toBeGreaterThanOrEqual(44);
    await expect.poll(() => passageInView(page, id)).toBe(true);
  }
  await box.getByRole('button', { name: 'Comment', exact: true }).click();
  await expect(box.getByRole('alert')).toHaveText(REFUSAL);
  await expect(commentEditor(page)).toHaveValue('Refused once');
  await expect(box.getByRole('button', { name: 'Retry', exact: true })).toHaveCount(1);
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  await frame(box, 'failed-post');

  // Retry posts the same text from the same box.
  refused = null;
  await box.getByRole('button', { name: 'Retry', exact: true }).click();
  await commentOnMessage(row, 'First phrase');
  await expect(commentEditor(page)).toHaveCount(0);
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  await frame(row, 'retried-post');

  // Stored, then failed: queued behind the held turn, its input is ended as undeliverable. The box closes, and the
  // comment's own thread, opened from its highlight, holds chat's label for a failed message and chat's Retry.
  await selectPhrase(page, id, 'Second phrase');
  await commentEditor(page).fill('Fails after queued');
  await box.getByRole('button', { name: 'Comment', exact: true }).click();
  await expect(commentEditor(page)).toHaveCount(0);
  await expect.poll(async () => (await posted(request, thread, 'Fails after queued')).length).toBe(1);
  const [queued] = await posted(request, thread, 'Fails after queued');
  await failQueuedInput(request, thread, queued.user_message_id, UNDELIVERED);
  const failed = await openCommentThread(row, page, 'Second phrase');
  await expect(failed.locator('.failed-reply')).toContainText(`Not delivered: ${UNDELIVERED}`);
  await expect(failed.locator('.failed-reply').getByRole('button', { name: 'Retry', exact: true })).toHaveCount(1);
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  await frame(failed, 'failed-after-queued');

  // That Retry is the one way back for the comment, so on touch it is a full target.
  const retry = failed.locator('.failed-reply').getByRole('button', { name: 'Retry', exact: true });
  await touch(page);
  // Measure the settled box: the 1440 popover opens from scale(0.98) (popoverMotion), so mid-open it reads 43.12.
  await retry.evaluate(element => {
    const running: Animation[] = [];
    for (let node: Element | null = element; node; node = node.parentElement) running.push(...node.getAnimations());
    return Promise.all(running.filter(animation => animation.effect?.getComputedTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => undefined)));
  });
  const target = (await retry.boundingBox())!;
  expect(target.height).toBeGreaterThanOrEqual(44);
  expect(target.width).toBeGreaterThanOrEqual(44);
  await frame(failed, 'failed-retry-touch');

  // Pressing it twice posts the comment once more through the comment route, and nothing reaches the transcript.
  await retryTwice(page, retry);
  await postedOnceMore(request, thread, 'Fails after queued', queued.user_message_id);
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  await frame(failed, 'failed-retry-pressed');
  await release(request, `held-${width}`);
});

test('a comment stopped after its turn began writing reads uncertain and retries once, 1440', async ({ page, request }) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1' || test.info().project.name !== 'chromium-desktop', 'Requires a disposable mock instance');
  if (framesDirectory) fs.mkdirSync(framesDirectory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  const title = 'Comment uncertain 1440';
  const thread = await createSession(request, title);
  const id = await injectMessage(request, thread, { role: 'assistant', content: MESSAGE, metadata: { for_user: true } }, TEST_TOKEN);
  // The turn reads a file, takes the comment at the boundary after that call, and is stopped while its next model call is open.
  await seedMockResponses(request, [
    { tool_calls: [{ id: 'read-uncertain', name: 'read_file', arguments: JSON.stringify({ path: 'README.md' }) }], finish_reason: 'tool_calls', response_gate: 'uncertain-start' },
    { content: 'Never written.', response_gate: 'uncertain-write' },
    { content: 'Finished.' },
  ]);
  await openAppPastLanding(page);
  await sidebarThread(page, title).click();
  await waitForWsConnected(page);
  const row = page.locator(`[data-message-id="${id}"]`);
  await expect(row).toBeVisible();
  await sendChatMessage(page, 'Read the readme.');
  await expect(page.locator('.messages .message.user')).toHaveCount(1);

  await selectPhrase(page, id, 'Second phrase');
  await commentEditor(page).fill('Stopped mid-write');
  await page.getByRole('dialog', { name: 'New comment' }).getByRole('button', { name: 'Comment', exact: true }).click();
  await expect(commentEditor(page)).toHaveCount(0);
  await expect.poll(async () => (await posted(request, thread, 'Stopped mid-write')).length).toBe(1);
  const [sent] = await posted(request, thread, 'Stopped mid-write');
  await release(request, 'uncertain-start');
  await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBe(2);
  await page.getByRole('button', { name: 'Stop turn' }).click();

  const uncertain = await openCommentThread(row, page, 'Second phrase');
  await expect(uncertain.locator('.failed-reply')).toContainText('Delivery uncertain');
  const retry = uncertain.locator('.failed-reply').getByRole('button', { name: 'Retry anyway', exact: true });
  await expect(retry).toHaveCount(1);
  await expect(page.locator('.messages .message.user')).toHaveCount(1);
  if (framesDirectory) await capture(page, uncertain, 'uncertain-after-stop-1440');

  await retryTwice(page, retry);
  await postedOnceMore(request, thread, 'Stopped mid-write', sent.user_message_id);
  await expect(page.locator('.messages .message.user')).toHaveCount(1);
  if (framesDirectory) await capture(page, uncertain, 'uncertain-retry-pressed-1440');
  await request.post(`${backend}/api/test/mock-gates/uncertain-write/release`);
});

test('the comment popover leaves its passage uncovered, keeps focus until Escape, and keeps its text after Escape, 900', async ({ page, request }) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1' || test.info().project.name !== 'chromium-desktop', 'Requires a disposable mock instance');
  await page.setViewportSize({ width: 900, height: 900 });
  const title = 'Comment popover focus';
  const thread = await createSession(request, title);
  const id = await injectMessage(request, thread, { role: 'assistant', content: MESSAGE, metadata: { for_user: true } }, TEST_TOKEN);
  await openAppPastLanding(page);
  await sidebarThread(page, title).click();
  await waitForWsConnected(page);
  await expect(page.locator(`[data-message-id="${id}"]`)).toBeVisible();
  await selectPhrase(page, id, 'Second phrase');
  // Without the margin column the comment is written in a popover, which sits beside the passage, never over it.
  const popover = page.locator('.comment-dialog[role="dialog"]');
  await expect(popover).toBeVisible();
  await commentEditor(page).fill('Typed before tabbing');
  await expect.poll(() => page.evaluate(id => {
    const mark = document.querySelector<HTMLElement>(`[data-message-id="${id}"] .cm-comment-pending:not([role="button"])`);
    const card = document.querySelector('[data-popover]')?.getBoundingClientRect();
    if (!mark || !card) return 'missing';
    const rect = mark.getBoundingClientRect();
    return rect.bottom <= card.top || rect.top >= card.bottom || rect.right <= card.left || rect.left >= card.right ? 'clear' : 'covered';
  }, id)).toBe('clear');
  // Tab and Shift+Tab cycle inside the box, with text typed so Comment is enabled.
  const inside = () => popover.evaluate(element => element.contains(document.activeElement));
  for (const key of ['Tab', 'Tab', 'Tab', 'Tab', 'Shift+Tab', 'Shift+Tab', 'Shift+Tab', 'Shift+Tab']) {
    await page.keyboard.press(key);
    expect(await inside()).toBe(true);
  }
  // Escape closes the box like a click outside: the typed text stays with its passage; only Cancel discards it.
  await page.keyboard.press('Escape');
  await expect(popover).toHaveCount(0);
  await selectPhrase(page, id, 'Second phrase');
  await expect(commentEditor(page)).toHaveValue('Typed before tabbing');
  await commentEditor(page).press('Escape');
  await expect(popover).toHaveCount(0);

  // On touch the selection pill is a full target at any width.
  await touch(page);
  await selectPhrase(page, id, 'First phrase', false);
  await expect.poll(() => page.locator('.sel-comment').evaluate(element => (element as HTMLElement).offsetHeight)).toBeGreaterThanOrEqual(44);
  if (framesDirectory) { fs.mkdirSync(framesDirectory, { recursive: true }); await capture(page, page.locator('.sel-comment'), 'pill-touch-900'); }
});
