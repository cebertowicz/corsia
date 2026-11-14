import path from 'node:path';
import { expect, test } from './support/test';
import { createSession, openAppPastLanding, sidebarThread, waitForWsConnected, TEST_TOKEN } from './support/corsia';
import { injectMessage, releaseMockGate, seedMockResponses } from './support/mock';
import { setFocusMode } from './support/focusMode';
import { closeCommentThread, commentEditor, commentEditorClosed, commentOnMessage, postedComments } from './support/chatComments';

test.use({ seedPerTest: true });

test.afterEach(async ({ request }) => {
  await setFocusMode(request, true);
});

for (const mode of ['normal', 'focus', 'focus-collapsed'] as const) test(`chat answers land on comments and the jump control opens them (${mode})`, async ({ page, request }) => {
  const focus = mode !== 'normal';
  test.skip(process.env.CORSIA_E2E_MOCK !== '1' || test.info().project.name === 'chromium-mobile');
  test.setTimeout(120_000);
  await setFocusMode(request, focus);
  const thread = await createSession(request, 'Threaded answers proof');
  const target = await injectMessage(request, thread, { role: 'assistant', content: 'The selected phrase explains the result.', metadata: mode === 'focus-collapsed' ? {} : { for_user: true } }, TEST_TOKEN);
  for (let i = 0; i < (mode === 'normal' ? 55 : 40); i++) await injectMessage(request, thread, { role: 'assistant', content: `Later update ${i}`, metadata: { for_user: true } }, TEST_TOKEN);
  await openAppPastLanding(page);
  await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
  await sidebarThread(page, 'Threaded answers proof').click();
  await waitForWsConnected(page);
  const row = page.locator(`[data-message-id="${target}"]`);
  if (mode === 'normal') {
    await page.getByRole('button', { name: 'Load older messages' }).dispatchEvent('click');
    await expect(row).toBeAttached();
  }
  if (mode === 'focus-collapsed') {
    const work = page.locator('[data-work-block]').filter({ has: page.getByRole('button', { name: /Expand (Worked|Background work)/ }) });
    await expect(work).toHaveCount(1);
    await work.getByRole('button', { name: /Expand (Worked|Background work)/ }).click();
  }
  // The comment posts as soon as it is written; its answer is sent with post_update and held until the comment is out of view, so it arrives unread.
  await seedMockResponses(request, [
    { content: '', completion_gate: 'threaded-answer', tool_calls: [{ id: 'update-1', name: 'post_update', arguments: JSON.stringify({ text: '[c2.1] This is the answer.' }) }], finish_reason: 'tool_calls' },
    { content: 'Done.' },
  ]);
  await row.scrollIntoViewIfNeeded();
  await page.evaluate((id) => {
    const body = document.querySelector<HTMLElement>(`[data-message-id="${id}"] .long-message-body`)!;
    const quote = 'The selected phrase';
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
  }, target);
  await page.locator('.sel-comment').click();
  await commentEditor(page).fill('Why?');
  await commentEditor(page).press('Control+Enter');
  // The comment popover closes on a transition; collapsing or scrolling under it leaves the next surface unopened.
  await commentEditorClosed(page);
  // It stays on its highlight and never becomes a chat message.
  await commentOnMessage(row, 'The selected phrase');
  await expect(page.locator('.messages .message.user')).toHaveCount(0);
  if (mode === 'focus-collapsed') {
    const work = row.locator('xpath=ancestor::section[@data-work-block]');
    await expect(work).toHaveCount(1);
    await work.getByRole('button', { name: /Collapse (Worked|Background work)/ }).click();
  } else {
    const scroller = page.locator('.messages');
    await expect(scroller).toHaveCount(1);
    await page.getByText(`Later update ${mode === 'normal' ? 54 : 39}`, { exact: true }).scrollIntoViewIfNeeded();
    await expect.poll(async () => {
      const [targetBox, scrollBox] = await Promise.all([row.boundingBox(), scroller.boundingBox()]);
      return !!targetBox && !!scrollBox && targetBox.y + targetBox.height <= scrollBox.y;
    }).toBe(true);
  }
  await releaseMockGate(request, 'threaded-answer');
  let answerId = '';
  await expect.poll(async () => {
    const response = await request.get(`/api/threads/${thread}/messages?token=${encodeURIComponent(TEST_TOKEN)}`);
    const body = await response.json();
    const [sent] = postedComments(body.messages);
    const answer = body.messages.flatMap((message: any) => message.chat_comments ?? []).flatMap((c: any) => c.answers ?? [])
      .find((a: any) => a.author_kind === 'agent' && a.text === 'This is the answer.');
    answerId = answer?.assistant_message_id ?? '';
    // The answer is in the comment's thread and the step that wrote it is in no page read: it draws nothing.
    return { cid: sent?.cid, answered: Boolean(answerId), inChat: body.messages.some((message: any) => message.id === answerId) };
  }, { timeout: 30_000 }).toEqual({ cid: 'c2.1', answered: true, inChat: false });
  await expect(page.locator(`[data-message-id="${answerId}"]`)).not.toBeVisible();
  if (mode === 'normal') {
    await page.reload();
    await sidebarThread(page, 'Threaded answers proof').click();
    await expect(row).toHaveCount(0);
  }
  const frames = process.env.CHAT_COMMENT_FRAMES ?? path.resolve('test-results/chat-threaded-answers');
  // The chat's jump control says a new reply is waiting and jumps to it.
  const control = page.getByRole('button', { name: /next new reply/ });
  await expect(control).toBeVisible({ timeout: 30_000 });
  await expect(control).toContainText('1 new');
  // The comment sits earlier in the transcript, so the control points up: to an unread reply above, or to the older page holding it.
  await expect(control).toHaveAccessibleName(mode === 'normal' ? /next new reply in older messages/ : /next new reply above/);
  if (mode === 'focus') {
    await page.locator('.messages').evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await expect.poll(async () => {
      const [box, scroller] = await Promise.all([row.boundingBox(), page.locator('.messages').boundingBox()]);
      return !!box && !!scroller && box.y + box.height <= scroller.y;
    }).toBe(true);
    // Read from the bottom of the transcript, the unread reply is still announced and still points up.
    await expect(control).toHaveAccessibleName(/next new reply above/);
    const spacing = await page.evaluate((answer) => {
      const scroller = document.querySelector<HTMLElement>('.messages')!;
      const toggle = [...scroller.querySelectorAll<HTMLElement>('.fold-toggle')].at(-1);
      return {
        paddingBottom: getComputedStyle(scroller).paddingBottom,
        scrollerBottom: scroller.getBoundingClientRect().bottom,
        lastWorkBottom: toggle?.getBoundingClientRect().bottom,
        pendingRegions: scroller.querySelectorAll('.pending-input-region').length,
        answerRows: scroller.querySelectorAll(`[data-message-id="${CSS.escape(answer)}"]`).length,
      };
    }, answerId);
    console.log('focus-spacing', JSON.stringify(spacing));
  }
  await page.screenshot({ path: path.join(frames, `unread-${mode}.png`), animations: 'disabled' });
  // Activating it lands on the comment and opens its thread, where the answer reads.
  // The jump is a pick: it records the answer read as its thread opens.
  const viewed = page.waitForResponse((response) => response.request().method() === 'POST' && /\/comments\/[^/]+\/viewed$/.test(new URL(response.url()).pathname) && response.ok());
  await control.click();
  // Nothing is added under the message: the thread is the surface the highlight opens.
  await expect(row.locator('.chat-comment-thread')).toHaveCount(0);
  const surface = page.getByRole('dialog', { name: 'Comment thread', exact: true });
  await expect(surface).toBeVisible();
  const card = surface.locator('[data-chat-comment="c2.1"]');
  await expect(card).toContainText('This is the answer.');
  await expect(card.locator('.ct-msg-author')).toHaveText('CORSIA');
  await expect(card.locator('.ct-msg-author')).not.toHaveText(thread);
  await page.screenshot({ path: path.join(frames, `landed-${mode}.png`), animations: 'disabled' });
  // Opening the thread marks the reply read: the control's notice goes at once, while the highlight keeps its unread mark
  // until the thread closes, so the sign the reader came to see does not vanish under them.
  await expect(page.getByRole('button', { name: /next new reply/ })).toHaveCount(0, { timeout: 10_000 });
  await expect(row.locator('[data-highlight-dot]')).toHaveCount(1);
  await viewed;
  await closeCommentThread(page, surface);
  await expect(row.locator('[data-highlight-dot]')).toHaveCount(0);
  await page.screenshot({ path: path.join(frames, `read-${mode}.png`), animations: 'disabled' });
});
