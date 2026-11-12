import fs from 'node:fs';
import { expect, test, type APIRequestContext, type Page } from './support/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, seedMockResponses } from './support/mock';
import { commentEditor, commentOnMessage } from './support/chatComments';
import { capture, framesDirectory, selectPhrase } from './support/chatCommentFrames';

/**
 * A comment's post failing, seen where the person wrote it: refused before it is stored, the box keeps the text and
 * says why, and its one Retry posts it; stored and then failed, the box closes and the comment's own thread shows
 * the failure with its Retry. The socket is routed so each failure lands on the exact post under test.
 */
const backend = `http://127.0.0.1:${Number(process.env.CORSIA_E2E_BACKEND_PORT ?? 3110)}`;
test.describe.configure({ timeout: 180_000 });
test.use({ seedPerTest: true, colorScheme: 'light', video: 'off' });

const MESSAGE = 'First phrase is refused once. Second phrase fails after it is stored.';
// The server's own words when it refuses a send because the chat's model is down.
const REFUSAL = 'Model unavailable: the provider is not responding. Please select a different model.';

type Fault = { note: string; kind: 'refuse' | 'fail-after-queued' };

/** Route the chat socket so the frame carrying `fault().note` is refused, or is stored and then reported failed. */
async function routeFaults(page: Page, fault: () => Fault | null) {
  await page.routeWebSocket(/\/ws(\?|$)/, socket => {
    const server = socket.connectToServer();
    const failing = new Set<string>();
    server.onMessage(frame => {
      if (typeof frame === 'string' && failing.size) {
        const data = JSON.parse(frame);
        if (data.type === 'input_queued' && failing.delete(data.input_id)) {
          socket.send(JSON.stringify({ ...data, delivery_state: 'failed', delivery_error: 'The accepted input did not reach a Turn.' }));
          return;
        }
      }
      socket.send(frame);
    });
    socket.onMessage(frame => {
      const now = fault();
      if (now && typeof frame === 'string' && frame.includes(now.note)) {
        const data = JSON.parse(frame);
        if (now.kind === 'refuse') {
          socket.send(JSON.stringify({ type: 'error', thread_id: data.thread_id, input_id: data.input_id, message: REFUSAL }));
          return;
        }
        failing.add(data.input_id);
      }
      server.send(frame);
    });
  });
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

async function release(request: APIRequestContext, gate: string) {
  expect((await request.post(`${backend}/api/test/mock-gates/${gate}/release`)).ok()).toBeTruthy();
}

for (const width of [1440, 393]) test(`a failed comment post keeps its text and retries once, ${width}`, async ({ page, request }) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1' || test.info().project.name !== 'chromium-desktop', 'Requires a disposable mock instance');
  if (framesDirectory) fs.mkdirSync(framesDirectory, { recursive: true });
  const frame = async (target: Parameters<typeof capture>[1], name: string) => { if (framesDirectory) await capture(page, target, `${name}-${width}`); };
  await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
  let fault: Fault | null = null;
  await routeFaults(page, () => fault);
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

  // Stored, then failed: the box closes and the comment's own thread opens on its failure and Retry.
  fault = { note: 'Fails after queued', kind: 'fail-after-queued' };
  await selectPhrase(page, id, 'Second phrase');
  await commentEditor(page).fill('Fails after queued');
  await box.getByRole('button', { name: 'Comment', exact: true }).click();
  await expect(commentEditor(page)).toHaveCount(0);
  const failed = page.getByRole('dialog', { name: 'Comment thread' }).or(page.locator('.chat-margin .chat-margin-card.selected'));
  await expect(failed).toBeVisible();
  await expect(failed.locator('.failed-reply')).toContainText('Comment not delivered');
  await expect(failed.locator('.failed-reply').getByRole('button', { name: 'Retry', exact: true })).toHaveCount(1);
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  await frame(failed, 'failed-after-queued');
  await release(request, `held-${width}`);
});

test('the comment popover leaves its passage uncovered and keeps focus until Escape, 900', async ({ page, request }) => {
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
  await page.keyboard.press('Escape');
  await expect(popover).toHaveCount(0);
});
