import path from 'node:path';
import { expect, test } from './support/test';
import { chatComposer, createSession, openAppPastLanding, sidebarThread, waitForWsConnected, TEST_TOKEN } from './support/corsia';
import { injectMessage, seedMockResponses } from './support/mock';
import { setFocusMode } from './support/focusMode';

test.use({ seedPerTest: true });

for (const mode of ['normal', 'focus', 'focus-collapsed'] as const) test(`chat answers land on comments and the reply chip opens them (${mode})`, async ({ page, request }) => {
  const focus = mode !== 'normal';
  test.skip(process.env.CORSIA_E2E_MOCK !== '1' || test.info().project.name === 'chromium-mobile');
  test.setTimeout(120_000);
  await setFocusMode(request, focus);
  const thread = await createSession(request, 'Threaded answers proof');
  const target = await injectMessage(request, thread, { role: 'assistant', content: 'The selected phrase explains the result.', metadata: mode === 'focus-collapsed' ? {} : { for_user: true } }, TEST_TOKEN);
  for (let i = 0; i < (mode === 'normal' ? 55 : 12); i++) await injectMessage(request, thread, { role: 'assistant', content: `Later update ${i}`, metadata: { for_user: true } }, TEST_TOKEN);
  await openAppPastLanding(page);
  await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
  await sidebarThread(page, 'Threaded answers proof').click();
  await waitForWsConnected(page);
  const row = page.locator(`[data-message-id="${target}"]`);
  if (mode === 'normal') {
    await page.getByRole('button', { name: 'Load older messages' }).click();
    await expect(row).toBeAttached();
  }
  if (mode === 'focus-collapsed') await page.getByRole('button', { name: /Expand (Worked|Background work)/ }).first().click();
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
  await page.getByPlaceholder('Comment…').fill('Why?');
  await page.getByPlaceholder('Comment…').press('Control+Enter');
  if (mode === 'focus-collapsed') await page.getByRole('button', { name: /Collapse (Worked|Background work)/ }).first().click();
  await seedMockResponses(request, [{ content: '[c2.1] This is the answer.' }]);
  await chatComposer(page).fill('Please answer the comment');
  await chatComposer(page).press('Enter');
  let answerId = '';
  await expect.poll(async () => {
    const response = await request.get(`/api/threads/${thread}/messages?token=${encodeURIComponent(TEST_TOKEN)}`);
    const body = await response.json();
    const sent = body.messages.find((message: any) => message.role === 'user' && message.metadata?.reply_context?.comments?.length);
    const answer = body.messages.find((message: any) => message.role === 'assistant' && message.content.includes('This is the answer.'));
    answerId = answer?.id ?? '';
    return { cid: sent?.metadata.reply_context.comments[0].cid, answer: answer?.content };
  }, { timeout: 30_000 }).toEqual({ cid: 'c2.1', answer: '[c2.1] This is the answer.' });
  await expect(page.locator(`[data-message-id="${answerId}"]`)).not.toBeVisible();
  if (mode === 'normal') {
    await page.reload();
    await sidebarThread(page, 'Threaded answers proof').click();
    await expect(row).toHaveCount(0);
  }
  const chip = page.getByText('1 new reply', { exact: false });
  await expect(chip).toBeVisible({ timeout: 30_000 });
  if (mode === 'focus') {
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
  const frames = process.env.CHAT_COMMENT_FRAMES!;
  await page.screenshot({ path: path.join(frames, `chip-${mode}.png`), animations: 'disabled' });
  await chip.click();
  const card = row.locator('[data-chat-comment="c2.1"]');
  await expect(card).toContainText('This is the answer.');
  await expect(card.locator('.ct-msg-author')).toHaveText('CORSIA');
  await expect(card.locator('.ct-msg-author')).not.toHaveText(thread);
  await page.screenshot({ path: path.join(frames, `landed-${mode}.png`), animations: 'disabled' });
  await expect(chip).toHaveCount(0, { timeout: 10_000 });
  await page.screenshot({ path: path.join(frames, `read-${mode}.png`), animations: 'disabled' });
});
