import { expect, test, type Page } from './support/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, seedMockResponses, setMockDeliveryDelay } from './support/mock';
import { commentEditor, commentEditorClosed, commentOnMessage, openCommentThread } from './support/chatComments';

/**
 * A sent comment or reply is in its box or in its thread, never in both. The server answers the post
 * once the input is stored; a slow provider start after that must not hold the box open while the
 * thread already shows the text.
 */
test.use({ viewport: { width: 1440, height: 900 } });
const DELAY_SECONDS = 2;

/** Sample every frame until `until` resolves; count the frames that showed `note` in a field and in a thread at once. */
async function framesWithBoth(page: Page, note: string, act: () => Promise<void>) {
  await page.evaluate((text) => {
    const state = { both: 0, inBox: 0, inThread: 0, running: true };
    (window as any).__sentOnce = state;
    const holds = (node: Element) => /^(TEXTAREA|INPUT)$/.test(node.tagName) && (node as HTMLTextAreaElement).value.includes(text);
    const tick = () => {
      const inBox = [...document.querySelectorAll('textarea, input')].some(holds);
      const inThread = [...document.querySelectorAll('.chat-margin-card, [role="dialog"][aria-label="Comment"]')]
        .some((card) => (card.textContent ?? '').includes(text));
      if (inBox) state.inBox++;
      if (inThread) state.inThread++;
      if (inBox && inThread) state.both++;
      if (state.running) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }, note);
  await act();
  return page.evaluate(() => {
    const state = (window as any).__sentOnce;
    state.running = false;
    return state as { both: number; inBox: number; inThread: number };
  });
}

test('a sent comment and its reply are never in the box and the thread together', async ({ page, request }) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1' || test.info().project.name === 'chromium-mobile', 'Requires disposable desktop mock instance');
  test.setTimeout(120_000);
  const quote = 'selected phrase';
  const title = 'Sent once';
  const thread = await createSession(request, title);
  const id = await injectMessage(request, thread, { role: 'assistant', content: 'A selected phrase for a comment.', metadata: { for_user: true } }, TEST_TOKEN);
  await openAppPastLanding(page);
  await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
  await sidebarThread(page, title).click();
  await waitForWsConnected(page);
  const row = page.locator(`[data-message-id="${id}"]`);
  await expect(row).toBeVisible();
  await row.evaluate((el, phrase) => {
    const walker = document.createTreeWalker(el.querySelector('.long-message-body')!, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode() as Text | null;
    while (node && !node.data.includes(phrase)) node = walker.nextNode() as Text | null;
    if (!node) throw new Error('Selected text was not rendered');
    const start = node.data.indexOf(phrase);
    const range = document.createRange(); range.setStart(node, start); range.setEnd(node, start + phrase.length);
    const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
  }, quote);
  await page.locator('.sel-comment').click();
  await commentEditor(page).fill('First comment');

  // The provider start after the input is stored takes seconds, as on a loaded machine.
  await seedMockResponses(request, [{ content: 'Received comment' }, { content: 'Received reply' }]);
  await setMockDeliveryDelay(request, DELAY_SECONDS);
  try {
    const root = await framesWithBoth(page, 'First comment', async () => {
      await page.getByRole('button', { name: 'Comment', exact: true }).click();
      await commentEditorClosed(page);
      await commentOnMessage(row, quote);
    });
    expect(root.inThread, 'the comment reaches its thread').toBeGreaterThan(0);
    expect(root.both, 'frames that showed the comment in its box and its thread at once').toBe(0);

    const surface = await openCommentThread(row, page, quote);
    await expect(surface.getByText('First comment')).toBeVisible();
    await surface.getByPlaceholder('Reply…').fill('Draft reply');
    const reply = await framesWithBoth(page, 'Draft reply', async () => {
      await surface.getByRole('button', { name: 'Send reply' }).click();
      await expect(surface.getByText('Draft reply')).toBeVisible();
      await expect(surface.getByPlaceholder('Reply…')).toHaveValue('');
    });
    expect(reply.inThread, 'the reply reaches its thread').toBeGreaterThan(0);
    expect(reply.both, 'frames that showed the reply in its field and its thread at once').toBe(0);
  } finally {
    await setMockDeliveryDelay(request, 0);
  }
});
