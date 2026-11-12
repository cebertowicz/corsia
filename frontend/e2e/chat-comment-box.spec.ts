import fs from 'node:fs';
import { expect, test, type APIRequestContext, type Page } from './support/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, seedMockResponses } from './support/mock';
import { commentEditor, commentOnMessage, openCommentThread } from './support/chatComments';
import { capture, framesDirectory, selectPhrase } from './support/chatCommentFrames';

/**
 * A comment's post failing, seen where the person wrote it: refused before it is stored, the box keeps the text and
 * says why, and its one Retry posts it; stored and then failed, the box closes and the comment's own thread, opened
 * from its highlight, shows the failure with its Retry. The comment route is routed so each failure lands on the exact
 * post under test: a refusal is the route's own error answer, and a stored post that then failed is the server's answer
 * with that comment's delivery marked failed, as its row reads when delivery fails.
 */
const backend = `http://127.0.0.1:${Number(process.env.CORSIA_E2E_BACKEND_PORT ?? 3110)}`;
test.describe.configure({ timeout: 180_000 });
test.use({ seedPerTest: true, colorScheme: 'light', video: 'off' });

const MESSAGE = 'First phrase is refused once. Second phrase fails after it is stored.';
// The server's own words when it refuses a send because the chat's model is down.
const REFUSAL = 'Model unavailable: the provider is not responding. Please select a different model.';

const UNDELIVERED = 'The accepted input did not reach a Turn.';

type Fault = { note: string; kind: 'refuse' | 'fail-after-queued' };

/** Mark the comment, or the person's reply, written as `note` failed wherever an answer carries it. */
function markFailed(value: unknown, note: string): void {
  if (Array.isArray(value)) { value.forEach(item => markFailed(item, note)); return; }
  if (!value || typeof value !== 'object') return;
  const row = value as Record<string, unknown>;
  const comment = Array.isArray(row.answers) && row.note === note;
  const reply = row.author_kind === 'user' && row.text === note;
  if (comment || reply) Object.assign(row, { delivery_state: 'failed', delivery_error: UNDELIVERED });
  Object.values(row).forEach(child => markFailed(child, note));
}

/** Route the comment post and the reads of its thread so `fault().note` is refused, or is stored and then reads failed. */
async function routeFaults(page: Page, fault: () => Fault | null) {
  const failing = new Set<string>();
  await page.route(/\/api\/threads\/[^/]+\/(chat-comments|chat-comment-threads|messages)(\?|$)/, async route => {
    const now = fault();
    const posted = route.request().postData() ?? '';
    const post = new URL(route.request().url()).pathname.endsWith('/chat-comments');
    if (now?.kind === 'refuse' && post && posted.includes(now.note)) {
      await route.fulfill({ status: 503, json: { detail: REFUSAL } });
      return;
    }
    if (now?.kind === 'fail-after-queued' && post && posted.includes(now.note)) failing.add(now.note);
    if (!failing.size) { await route.fallback(); return; }
    const response = await route.fetch();
    const json = await response.json();
    for (const note of failing) markFailed(json, note);
    await route.fulfill({ response, json });
  });
  return { stop: () => failing.clear() };
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
  let fault: Fault | null = null;
  const faults = await routeFaults(page, () => fault);
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
  fault = { note: 'Refused once', kind: 'refuse' };
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
  fault = null;
  await box.getByRole('button', { name: 'Retry', exact: true }).click();
  await commentOnMessage(row, 'First phrase');
  await expect(commentEditor(page)).toHaveCount(0);
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  await frame(row, 'retried-post');

  // Stored, then failed: the box closes, and the comment's own thread, opened from its highlight, holds its failure and Retry.
  fault = { note: 'Fails after queued', kind: 'fail-after-queued' };
  await selectPhrase(page, id, 'Second phrase');
  await commentEditor(page).fill('Fails after queued');
  await box.getByRole('button', { name: 'Comment', exact: true }).click();
  await expect(commentEditor(page)).toHaveCount(0);
  const failed = await openCommentThread(row, page, 'Second phrase');
  await expect(failed.locator('.failed-reply')).toContainText('Comment not delivered');
  await expect(failed.locator('.failed-reply').getByRole('button', { name: 'Retry', exact: true })).toHaveCount(1);
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  await frame(failed, 'failed-after-queued');

  // That Retry is the one way back for the comment, so on touch it is a full target.
  const retry = failed.locator('.failed-reply').getByRole('button', { name: 'Retry', exact: true });
  await touch(page);
  const target = (await retry.boundingBox())!;
  expect(target.height).toBeGreaterThanOrEqual(44);
  expect(target.width).toBeGreaterThanOrEqual(44);
  await frame(failed, 'failed-retry-touch');

  // Pressing it sends nothing the transcript shows: the server's answer for the comment's row never becomes a chat bubble.
  faults.stop();
  const retried = page.waitForResponse(response => /\/messages\/[^/]+\/retry$/.test(new URL(response.url()).pathname));
  await retry.click();
  const response = await retried;
  expect(response.ok()).toBeTruthy();
  await response.finished();
  await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  await frame(failed, 'failed-retry-pressed');
  await release(request, `held-${width}`);
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
