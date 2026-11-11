import fs from 'node:fs';
import path from 'node:path';
import { expect, test, type Locator, type Page } from './support/test';
import { chatComposer, createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, seedMockResponses } from './support/mock';
import { closeCommentThread, commentEditor, commentOnMessage, openCommentThread } from './support/chatComments';

const directory = process.env.CHAT_COMMENT_DIRECT_FRAMES;
const backend = `http://127.0.0.1:${Number(process.env.CORSIA_E2E_BACKEND_PORT ?? 3110)}`;
test.describe.configure({ timeout: 120_000 });
test.use({ seedPerTest: true, colorScheme: 'light', video: 'off' });

async function capture(page: Page, target: Locator, name: string) {
  await expect(target).toBeVisible();
  await expect.poll(() => target.evaluate(element => {
    let opacity = 1;
    for (let node: HTMLElement | null = element as HTMLElement; node; node = node.parentElement) opacity *= Number(getComputedStyle(node).opacity);
    return opacity;
  })).toBe(1);
  const geometry = await target.evaluate(element => {
    const rect = element.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, viewportWidth: innerWidth, viewportHeight: innerHeight, documentWidth: document.documentElement.scrollWidth };
  });
  expect(geometry.width).toBeGreaterThan(0);
  expect(geometry.height).toBeGreaterThan(0);
  expect(geometry.x).toBeGreaterThanOrEqual(0);
  expect(geometry.x + geometry.width).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  const stamp = await page.locator('.version-overlay').innerText();
  console.log(`FRAME ${name} ${JSON.stringify({ stamp, geometry })}`);
  await page.screenshot({ path: path.join(directory!, `${name}.png`), animations: 'disabled' });
}

for (const width of [1440, 900, 393]) test(`direct comment frames ${width}`, async ({ page, request }) => {
  test.skip(!directory || process.env.CORSIA_E2E_MOCK !== '1' || test.info().project.name !== 'chromium-desktop', 'Requires a disposable mock instance and capture directory');
  fs.mkdirSync(directory!, { recursive: true });
  await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
  const thread = await createSession(request, `Direct frames ${width}`);
  const id = await injectMessage(request, thread, { role: 'assistant', content: 'Selected phrase for a comment.', metadata: { for_user: true } }, TEST_TOKEN);
  await seedMockResponses(request, [
    { content: '', tool_calls: [{ id: 'update-1', name: 'post_update', arguments: JSON.stringify({ text: 'Text for the chat.\n\n[c2.1] Answer in the comment.' }) }], finish_reason: 'tool_calls', response_gate: `comment-${width}` },
    { content: 'Finished working.' },
  ]);
  await openAppPastLanding(page);
  if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await sidebarThread(page, `Direct frames ${width}`).click();
  await waitForWsConnected(page);
  const row = page.locator(`[data-message-id="${id}"]`);
  await expect(row).toBeVisible();
  await page.evaluate(messageId => {
    const body = document.querySelector<HTMLElement>(`[data-message-id="${messageId}"] .long-message-body`)!;
    const quote = 'Selected phrase', start = body.textContent!.indexOf(quote);
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT), range = document.createRange();
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
  const composer = page.getByRole('dialog', { name: 'New comment' });
  await capture(page, composer, `before-post-${width}`);
  await composer.getByRole('button', { name: 'Comment', exact: true }).click();
  await commentOnMessage(row, 'Selected phrase');
  await expect(commentEditor(page)).toHaveCount(0);
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  const card = await openCommentThread(row, page, 'Selected phrase');
  await expect(card).toContainText('My direct comment');
  await capture(page, card, `after-post-${width}`);
  expect((await request.post(`${backend}/api/test/mock-gates/comment-${width}/release`)).ok()).toBeTruthy();
  await expect(card).toContainText('Answer in the comment.', { timeout: 30000 });
  await expect.poll(async () => (await page.locator('.messages .message').allInnerTexts()).join('\n')).not.toContain('Answer in the comment.');
  await capture(page, card, `after-answer-${width}`);
  await closeCommentThread(page, card);
  await seedMockResponses(request, [{ content: 'Follow-up received.' }]);
  await chatComposer(page).fill('A plain follow-up');
  await chatComposer(page).press('Enter');
  const bubble = page.locator('.messages .message.user').filter({ hasText: 'A plain follow-up' });
  await expect(bubble).toBeVisible();
  await expect(bubble.locator('.sent-comment')).toHaveCount(0);
  const mixedId = await injectMessage(request, thread, { role: 'user', content: 'Text sent with a comment', metadata: { reply_context: { comments: [{ id: 'frame-comment', seq: 1, from_off: 0, to_off: 15, quote: 'Selected phrase', anchor_prefix: '', anchor_suffix: ' for a comment.', note: 'This note stays in its thread', cid: 'c99.1' }] } } }, TEST_TOKEN);
  await page.reload();
  if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await sidebarThread(page, `Direct frames ${width}`).click();
  const mixed = page.locator(`[data-message-id="${mixedId}"]`);
  await expect(mixed).toContainText('Text sent with a comment');
  await expect(mixed).not.toContainText('This note stays in its thread');
  await expect(mixed.locator('.sent-comment')).toHaveCount(0);
  await capture(page, mixed, `comments-plus-text-${width}`);
});
