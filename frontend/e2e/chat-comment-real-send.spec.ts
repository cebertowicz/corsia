import path from 'node:path';
import { expect, test, type Page } from './support/test';
import { chatComposer, createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage, seedMockResponses } from './support/mock';
import { commentEditor, commentEditorClosed, commentOnMessage, openCommentThread } from './support/chatComments';

const text = 'First selected phrase. Second selected phrase.';
const frames = process.env.CHAT_COMMENT_FRAMES ?? path.resolve('test-results/chat-comments-real');
test.use({ video: 'off', viewport: { width: 1440, height: 900 }, colorScheme: 'light', deviceScaleFactor: 2, seedPerTest: true });
test.describe.configure({ timeout: 120_000 });

async function select(page: Page, id: string, quote: string, note: string) {
  await page.evaluate(({ id, quote }) => {
    const row = document.querySelector<HTMLElement>(`[data-message-id="${id}"]`)!;
    row.scrollIntoView({ block: 'center' });
    const body = row.querySelector<HTMLElement>('.long-message-body')!;
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
  await expect(page.locator('.sel-comment')).toBeVisible();
  await page.locator('.sel-comment').click();
  await commentEditor(page).fill(note);
  await commentEditor(page).press('Control+Enter');
  // The close transition clears the editing state when it ends; selecting or sending before that leaves the next surface unopened.
  await commentEditorClosed(page);
}

for (const scenario of ['two-comments', 'comments-only', 'subagent'] as const) {
  test(scenario, async ({ page, request }) => {
    test.skip(process.env.CORSIA_E2E_MOCK !== '1' || test.info().project.name === 'chromium-mobile', 'Requires disposable desktop mock instance');
    const title = `Real send ${scenario}`;
    const parent = await createSession(request, title);
    let thread = parent;
    if (scenario === 'subagent') {
      const response = await request.post(`/api/delegation/threads?token=${encodeURIComponent(TEST_TOKEN)}`, {
        data: { title: 'Comment worker', model: 'mock/test-model', thread_kind: 'subagent', spawned_by: parent },
      });
      expect(response.ok()).toBeTruthy();
      const body = await response.json();
      thread = body.thread_id;
      expect(thread).toEqual(expect.any(String));
    }
    const id = await injectMessage(request, thread, { role: 'assistant', content: text, metadata: { for_user: true } }, TEST_TOKEN);
    await openAppPastLanding(page);
    await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
    await sidebarThread(page, title).click();
    await waitForWsConnected(page);
    if (scenario === 'subagent') {
      await page.getByRole('button', { name: /Expand (?:1 event|Worked)/ }).click();
      await page.getByText('Comment worker', { exact: true }).first().click();
    }
    await expect(page.locator(`[data-message-id="${id}"]`)).toBeVisible();
    // Each comment posts from its own box as it is written; typed text is a chat message of its own and carries no comment.
    const count = scenario === 'two-comments' ? 2 : 1;
    await seedMockResponses(request, Array.from({ length: count + 1 }, () => ({ content: 'Received comments' })));
    await select(page, id, 'First selected phrase', 'First note');
    if (scenario === 'two-comments') await select(page, id, 'Second selected phrase', 'Second note');
    const composer = scenario === 'subagent' ? page.getByLabel('Agent chat').getByRole('textbox', { name: 'Message CORSIA' }) : chatComposer(page);
    if (scenario !== 'comments-only') {
      await composer.fill('Please address the comments');
      await composer.press('Enter');
    }
    const userRows = scenario === 'comments-only' ? 0 : 1;
    const messageRow = page.locator(`[data-message-id="${id}"]`);
    let comments: any[] = [];
    let sentIds: string[] = [];
    await expect.poll(async () => {
      const response = await request.get(`/api/threads/${thread}/messages?token=${encodeURIComponent(TEST_TOKEN)}`);
      const body = await response.json();
      const sent = body.messages.filter((m: any) => m.role === 'user' && m.metadata?.reply_context?.comments?.length);
      comments = sent.flatMap((m: any) => m.metadata.reply_context.comments);
      sentIds = sent.map((m: any) => m.id);
      return comments.length;
    }).toBe(count);
    expect(new Set(comments.map((c: any) => c.cid)).size).toBe(count);
    for (const comment of comments) expect(comment.cid).toMatch(/^c[0-9]+\.[0-9]+$/);
    console.log(`STORED ${scenario} ${JSON.stringify(comments)}`);
    // A posted comment adds no user bubble: it lives on its highlight, and its note reads in the thread the highlight opens.
    for (const sentId of sentIds) await expect(page.locator(`[data-message-id="${sentId}"]`)).toHaveCount(0);
    await expect(page.locator('.messages .message.user')).toHaveCount(userRows);
    await expect(page.locator('.sent-comment')).toHaveCount(0);
    await commentOnMessage(messageRow, 'First selected phrase');
    if (scenario !== 'subagent') await expect(await openCommentThread(messageRow, page, 'First selected phrase')).toContainText('First note');
    await expect.poll(async () => {
      const response = await request.get(`/api/threads/${thread}/messages?token=${encodeURIComponent(TEST_TOKEN)}`);
      const body = await response.json();
      return body.messages.some((m: any) => m.role === 'assistant' && m.content === 'Received comments');
    }, { timeout: 30_000 }).toBe(true);
    await page.reload();
    await sidebarThread(page, title).click();
    if (scenario === 'subagent') {
      await page.getByRole('button', { name: /Expand (?:1 event|Worked)/ }).click();
      await page.getByText('Comment worker', { exact: true }).first().click();
    }
    // The same reading after a reload: no comment bubble was stored on screen, and the highlight still carries the comment.
    await expect(page.locator('.messages .message.user')).toHaveCount(userRows);
    await expect(page.locator('.sent-comment')).toHaveCount(0);
    await commentOnMessage(messageRow, 'First selected phrase');
    if (scenario !== 'subagent') await expect(await openCommentThread(messageRow, page, 'First selected phrase')).toContainText('First note');
    await page.screenshot({ path: path.join(frames, `real-send-${scenario}-1440-light.png`), animations: 'disabled' });
  });
}
