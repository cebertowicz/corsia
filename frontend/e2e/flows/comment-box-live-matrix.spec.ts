import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { agentComment, openLivePair, reviewerSession, suggest } from '../support/liveConcurrency';
import { seedLiveDocument } from '../support/liveDocument';
import { beginComment, insertPrefix } from '../support/liveCommentProof';
import { TEST_TOKEN } from '../support/corsia';

/** From the empty end of the line: a click on commented text would open its card instead. */
async function caretAtLineEnd(page: Page, quote: string) {
 const line = page.locator('.cm-line').filter({ hasText: quote });
 const size = (await line.boundingBox())!;
 await line.click({ position: { x: size.width - 4, y: size.height / 2 } });
}

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
   const input = reader.getByPlaceholder(box === 'draft' ? 'Comment…' : 'Reply…');
   await input.focus();
   await input.evaluate((field: HTMLTextAreaElement) => field.setSelectionRange(5, 5));
   if (action === 'far') {
    await insertPrefix(writer, Array.from({ length: 40 }, (_, line) => `prefix ${line}\n`).join(''));
    await expect(reader.locator('.cm-content')).toContainText('prefix 0');
   }
   if (action === 'gone') {
    await caretAtLineEnd(writer, quote);
    await writer.keyboard.press('Shift+Home'); await writer.keyboard.press('Backspace');
    await expect(reader.locator('.cm-content')).not.toContainText(quote);
   }
   if (action === 'above') { await insertPrefix(writer, 'remote prefix\n'); await expect(reader.locator('.cm-content')).toContainText('remote prefix'); }
   if (action === 'inside') {
    await caretAtLineEnd(writer, quote); await writer.keyboard.press('Home'); await writer.keyboard.press('ArrowRight'); await writer.keyboard.insertText('Z');
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
  } finally { await pair.close(); }
 });
}
