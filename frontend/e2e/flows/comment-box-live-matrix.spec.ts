import { test } from '../support/test';
import { expect } from '@playwright/test';
import { agentComment, openLivePair, reviewerSession, suggest } from '../support/liveConcurrency';
import { deleteRenderedLine, seedLiveDocument } from '../support/liveDocument';
import { beginComment, insertPrefix } from '../support/liveCommentProof';
import { TEST_TOKEN } from '../support/corsia';
import { readMockCallCount, readUnmatchedThreads, seedMockResponses } from '../support/mock';

// Each case edits a document; its delayed owner wake must not enter the next case's mock seed.
test.use({ seedPerTest: true });

const actions = ['above', 'far', 'gone', 'inside', 'reply', 'decision', 'comment', 'resolve'] as const;
const cases = actions.flatMap(action => [[action, 1440, 900] as const, ...(action === 'above' || action === 'far' || action === 'reply' ? [[action, 393, 852] as const] : [])]);

for (const box of ['draft', 'reply'] as const) for (const [action, width, height] of cases) {
 test(`@comment-box-matrix ${box} survives ${action} at ${width}`, async ({ browser, request }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  test.setTimeout(120_000);
  const quote = box === 'draft' ? 'draft target text' : 'thread target text';
  const doc = await seedLiveDocument(request, `steady-${box}-${action}-${width}`, 'lead text\ndraft target text\nthread target text\nsuggestion text\nother text\n');
  const agent = await reviewerSession(request, `${box}-${action}-${width}-${Date.now()}`);
  const root = await agentComment(request, doc, agent.id, 'thread target text', 'Existing thread');
  const suggestion = await suggest(request, doc, agent.id, 'suggestion text', 'replacement text', 'Existing suggestion');
  let changes = 0;
  const pair = await openLivePair(browser, doc, width, height, async context => {
   context.on('page', page => page.on('websocket', socket => socket.on('framereceived', frame => {
    try { if (JSON.parse(String(frame.payload)).type === 'doc_comment_changed') changes++; } catch { /* Other frame types. */ }
   })));
  });
  try {
   const { reader, writer } = pair;
   if (box === 'draft') await beginComment(reader, quote, 'draft body');
   else {
    await reader.locator('.cm-comment-anchor').filter({ hasText: quote }).click();
    await expect(reader.getByPlaceholder('Reply…')).toBeVisible();
    await reader.getByPlaceholder('Reply…').fill('draft body');
   }
   const input = reader.getByPlaceholder(box === 'draft' ? 'Comment, or @ to tag an agent' : 'Reply…');
   await input.focus();
   await input.evaluate((field: HTMLTextAreaElement) => field.setSelectionRange(5, 5));
   if (action === 'far') {
    await insertPrefix(writer, Array.from({ length: 40 }, (_, line) => `prefix ${line}\n`).join(''));
    await expect(reader.locator('.cm-content')).toContainText('prefix 0');
   }
   if (action === 'gone') {
    await deleteRenderedLine(writer, quote);
    await expect(writer.locator('.cm-content')).not.toContainText(quote);
    await expect(reader.locator('.cm-content')).not.toContainText(quote);
   }
   if (action === 'above') { await insertPrefix(writer, 'remote prefix\n'); await expect(reader.locator('.cm-content')).toContainText('remote prefix'); }
   if (action === 'inside') {
    await writer.getByText(quote, { exact: true }).first().click();
    if (box === 'reply') {
     await expect(writer.getByRole('dialog')).toBeVisible();
     await writer.keyboard.press('Escape');
     await expect(writer.getByRole('dialog')).toHaveCount(0);
    } else {
     await writer.keyboard.press('Home');
     await writer.keyboard.press('ArrowRight');
    }
    await expect(writer.locator('.cm-content')).toBeFocused();
    const offset = await writer.evaluate(() => window.getSelection()?.anchorOffset ?? -1);
    expect(offset).toBeGreaterThan(0);
    expect(offset).toBeLessThan(quote.length);
    await writer.keyboard.insertText('Z');
    await expect(reader.locator('.cm-content')).toContainText(quote.slice(0, offset) + 'Z' + quote.slice(offset));
   }
   let queuedReplyAt: number | undefined;
   if (action === 'reply') {
    const prior = changes;
    const response = await request.post('/api/delegation/docs/comments', { params: { token: TEST_TOKEN }, data: { parent_id: root, body: 'Remote reply', as_session_id: agent.id } });
    expect(response.ok(), await response.text()).toBeTruthy();
    if (box === 'reply') {
     expect((await response.json()).queued.position).toBe(1);
     queuedReplyAt = prior;
    } else await expect.poll(() => changes).toBeGreaterThan(prior);
   }
   if (action === 'decision') {
    const response = await request.post(`/api/comments/${suggestion}/decision`, { params: { token: TEST_TOKEN }, data: { decision: 'accept' } });
    expect(response.ok(), await response.text()).toBeTruthy();
    await expect(reader.locator('.cm-content')).toContainText('replacement text');
   }
   if (action === 'resolve') {
    const response = await request.patch(`/api/comments/${root}`, { params: { token: TEST_TOKEN }, data: { resolved: true } });
    expect(response.ok(), await response.text()).toBeTruthy();
    await expect.poll(() => changes).toBeGreaterThan(0);
   }
   if (action === 'comment') {
    await agentComment(request, doc, agent.id, 'other text', 'Remote comment');
    await expect(reader.locator('.cm-comment-anchor').filter({ hasText: 'other text' })).toBeVisible();
   }
   await expect(input).toBeVisible(); await expect(input).toHaveValue('draft body'); await expect(input).toBeFocused();
   // The phone sheet may still be rising, so the box has to reach the screen, not start on it.
   await expect.poll(async () => { const seen = (await input.boundingBox())!; return seen.y >= 0 && seen.y + seen.height <= height; }).toBe(true);
   await expect(reader.locator('[aria-hidden="true"][data-popover]')).toHaveCount(0);
   await input.pressSequentially('!'); await expect(input).toHaveValue('draft! body');
   // Enough text that the box, then the card, scroll on their own: neither may take the person out of the box.
   await reader.keyboard.insertText('\nmore'.repeat(40));
   await expect(input).toBeFocused();
   await expect(reader.locator('[aria-hidden="true"][data-popover]')).toHaveCount(0);
   if (queuedReplyAt !== undefined) {
    const draft = await input.inputValue();
    // Leaving wakes the agent to choose what to do with its saved write; it is not posted automatically.
    await seedMockResponses(request, [
     { match_thread: agent.id, tool_calls: [{ id: `post-${root}`, name: 'doc_comment_update', arguments: JSON.stringify({ comment_id: root, post_as_is: true }) }], finish_reason: 'tool_calls' },
     { match_thread: agent.id, content: 'Posted the reply.', finish_reason: 'stop' },
     // Posting an agent reply also delivers feedback to the document owner.
     { match_thread: doc.threadId, content: 'Saw the reply.', finish_reason: 'stop' },
    ]);
    if (width === 393) await reader.getByRole('button', { name: 'Close', exact: true }).click();
    else await reader.keyboard.press('Escape');
    await expect.poll(async () => {
     const listed = await request.get(`/api/artifacts/${doc.artifactId}/comments`, { params: { token: TEST_TOKEN } });
     expect(listed.ok()).toBeTruthy();
     const comments = (await listed.json()).comments as Array<{ id: string; replies: Array<{ body: string }> }>;
     return comments.find(comment => comment.id === root)?.replies.some(reply => reply.body === 'Remote reply') ?? false;
    }, { message: 'the queued reply posts after the writer turn releases' }).toBe(true);
    await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBe(3);
    await expect.poll(async () => {
     const status = await request.get(`/api/delegation/threads/${agent.id}/status`, { params: { token: TEST_TOKEN } });
     expect(status.ok()).toBeTruthy();
     const state = await status.json();
     return !state.turn_active && state.recent_messages.some((message: { content?: string }) => message.content === 'Posted the reply.');
    }, { timeout: 30_000, message: 'the queued reply turn finishes before reseeding' }).toBe(true);
    await expect.poll(async () => {
     const status = await request.get(`/api/delegation/threads/${doc.threadId}/status`, { params: { token: TEST_TOKEN } });
     expect(status.ok()).toBeTruthy();
     const state = await status.json();
     return !state.turn_active && state.recent_messages.some((message: { content?: string }) => message.content === 'Saw the reply.');
    }, { timeout: 30_000, message: 'the document owner finishes receiving the reply' }).toBe(true);
    expect(await readUnmatchedThreads(request)).toEqual([]);
    await reader.locator('.cm-comment-anchor').filter({ hasText: quote }).click();
    await expect(reader.getByText('Remote reply')).toBeVisible();
    await expect(reader.getByPlaceholder('Reply…')).toHaveValue(draft);
   }
  } finally { await pair.close(); }
 });
}
