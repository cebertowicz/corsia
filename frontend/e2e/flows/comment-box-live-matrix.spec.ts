import { test } from '../support/test';
import { expect } from '@playwright/test';
import { agentComment, openLivePair, reviewerSession, suggest } from '../support/liveConcurrency';
import { seedLiveDocument } from '../support/liveDocument';
import { beginComment, insertPrefix, selectLine } from '../support/liveCommentProof';
import { TEST_TOKEN } from '../support/corsia';

for (const box of ['draft', 'reply'] as const) for (const action of ['above', 'inside', 'reply', 'decision', 'comment'] as const) {
 test(`@comment-box-matrix ${box} survives ${action}`, async ({ browser, request }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  test.setTimeout(120_000);
  const quote = box === 'draft' ? 'draft target text' : 'thread target text';
  const doc = await seedLiveDocument(request, `steady-${box}-${action}`, 'lead text\ndraft target text\nthread target text\nsuggestion text\nother text\n');
  const agent = await reviewerSession(request, `${box}-${action}-${Date.now()}`);
  const root = await agentComment(request, doc, agent.id, 'thread target text', 'Existing thread');
  const suggestion = await suggest(request, doc, agent.id, 'suggestion text', 'replacement text', 'Existing suggestion');
  let changes = 0;
  const pair = await openLivePair(browser, doc, 1440, 900, async context => {
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
   const input = reader.getByPlaceholder(box === 'draft' ? 'Comment…' : 'Reply…');
   await input.focus();
   if (action === 'above') { await insertPrefix(writer, 'remote prefix\n'); await expect(reader.locator('.cm-content')).toContainText('remote prefix'); }
   if (action === 'inside') {
    await selectLine(writer, quote); await writer.keyboard.press('Home'); await writer.keyboard.press('ArrowRight'); await writer.keyboard.insertText('Z');
    await expect(reader.locator('.cm-content')).toContainText(box === 'draft' ? quote.slice(0, 1) + 'Z' + quote.slice(1) : 'Z' + quote);
   }
   if (action === 'reply') {
    const prior = changes;
    const response = await request.post('/api/delegation/docs/comments', { params: { token: TEST_TOKEN }, data: { parent_id: root, body: 'Remote reply', as_session_id: agent.id } });
    expect(response.ok(), await response.text()).toBeTruthy();
    await expect.poll(() => changes).toBeGreaterThan(prior);
    if (box === 'reply') await expect(reader.getByText('Remote reply')).toBeVisible();
   }
   if (action === 'decision') {
    const response = await request.post(`/api/comments/${suggestion}/decision`, { params: { token: TEST_TOKEN }, data: { decision: 'accept' } });
    expect(response.ok(), await response.text()).toBeTruthy();
    await expect(reader.locator('.cm-content')).toContainText('replacement text');
   }
   if (action === 'comment') {
    await agentComment(request, doc, agent.id, 'other text', 'Remote comment');
    await expect(reader.locator('.cm-comment-anchor').filter({ hasText: 'other text' })).toBeVisible();
   }
   await expect(input).toBeVisible(); await expect(input).toHaveValue('draft body'); await expect(input).toBeFocused();
   await input.pressSequentially(' continued'); await expect(input).toHaveValue('draft body continued');
  } finally { await pair.close(); }
 });
}
