import { test } from '../support/test';
import { expect, type APIRequestContext, type Browser, type Page } from '@playwright/test';
import { createSession, loginByUrl, sendChatMessage, TEST_TOKEN } from '../support/corsia';
import { openLiveDocument, serverText, type LiveDocument } from '../support/liveDocument';
import { agentComment, reviewerSession, suggest } from '../support/liveConcurrency';
import { selectLine } from '../support/liveCommentProof';
import { agentCreatesSharedDocument, demoteAgent } from '../support/sharedLiveDocument';
import { readMockCallCount, seedMockResponses } from '../support/mock';

/**
 * A person types a new comment in a long document while other writers act on it: an agent edits and rewrites the file
 * through the file tools, another thread replies, suggests and comments. The box, its text and its focus stay, and no
 * key typed in the box reaches the document.
 */

const promoted: string[] = [];
test.beforeEach(({}, info) => { test.skip(info.project.name === 'chromium-mobile' || info.project.name === 'webkit-mobile'); });
test.afterEach(async ({ request }) => { for (const id of promoted.splice(0)) await demoteAgent(request, id); });

const quote = 'target line for the new comment';
const lines = (reword = false) => Array.from({ length: 60 }, (_, i) => i === 30 ? quote : `line ${i} ${reword ? 'reworded entirely' : 'with ordinary text'}`);
const body = (reword = false) => `# Long document\n\n${lines(reword).join('\n\n')}\n`;

async function open(browser: Browser, request: APIRequestContext, stamp: string, width: number, height: number) {
 const context = await browser.newContext({ viewport: { width, height } });
 const id = await createSession(request, `Writer ${stamp}`);
 promoted.push(id);
 const agentPage = await context.newPage();
 await agentPage.setViewportSize({ width: 1440, height: 900 });
 await loginByUrl(agentPage);
 await agentPage.locator('.thread-item').filter({ hasText: `Writer ${stamp}` }).click({ timeout: 10_000 });
 const agent = { id, title: `Writer ${stamp}`, page: agentPage };
 const live = await agentCreatesSharedDocument(request, agent, `outside-${stamp}`, body());
 const viewer = await context.newPage();
 await viewer.setViewportSize({ width, height });
 await openLiveDocument(viewer, live, 'line 0 with ordinary text');
 await viewer.bringToFront();
 return { context, viewer, agent, live };
}

/** The agent reads the document, then runs one file tool; resolves once the file has changed. */
async function agentWrites(request: APIRequestContext, agent: { id: string; page: Page }, live: LiveDocument, tool: Record<string, unknown>, name: 'edit_file' | 'write_file') {
 const stamp = `${Date.now()}`, before = await serverText(request, live);
 await seedMockResponses(request, [
  { tool_calls: [{ id: `read-${stamp}`, name: 'read_file', arguments: JSON.stringify({ path: live.docPath }) }], finish_reason: 'tool_calls', match_thread: agent.id },
  { tool_calls: [{ id: `${name}-${stamp}`, name, arguments: JSON.stringify({ path: live.docPath, ...tool }) }], finish_reason: 'tool_calls', match_thread: agent.id },
  { content: 'done', finish_reason: 'stop', match_thread: agent.id },
 ]);
 const calls = await readMockCallCount(request);
 await sendChatMessage(agent.page, 'Change the document.');
 await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBeGreaterThanOrEqual(calls + 3);
 await expect.poll(() => serverText(request, live), { timeout: 20_000 }).not.toBe(before);
}

for (const [width, height] of [[1440, 900], [393, 852]] as const) {
 test(`@comment-box-outside-write the new-comment box survives agent file writes and other threads' replies at ${width}`, async ({ browser, request }) => {
  test.setTimeout(240_000);
  const stamp = `${Date.now()}`;
  const { context, viewer, agent, live } = await open(browser, request, stamp, width, height);
  try {
   const other = await reviewerSession(request, `other-${stamp}`);
   promoted.push(other.id);
   const root = await agentComment(request, live, other.id, 'line 5 with ordinary text', 'Existing thread');
   await viewer.locator('.cm-scroller').evaluate(e => { e.scrollTop = e.scrollHeight * 0.45; });
   await selectLine(viewer, quote);
   await viewer.locator('.sel-comment').click();
   const input = viewer.getByPlaceholder('Comment…');
   await expect(input).toBeVisible();
   await input.fill('typing a comment');
   await viewer.evaluate(() => {
    const seen = { hidden: 0, blurs: 0 };
    (window as unknown as { __probe: typeof seen }).__probe = seen;
    new MutationObserver(() => { if (document.querySelector('[data-popover][inert], [data-popover][aria-hidden="true"], .sheet [inert]')) seen.hidden++; }).observe(document.body, { attributes: true, subtree: true, childList: true });
    document.addEventListener('focusout', event => { if ((event.target as HTMLElement).matches?.('textarea')) seen.blurs++; }, true);
   });
   let typed = 0;
   const still = async (step: string) => {
    await expect(input, step).toBeVisible();
    await expect(input, step).toBeFocused();
    await input.pressSequentially('.');
    typed++;
    await expect(input, step).toHaveValue(new RegExp(`^typing a comment\\.{${typed}}$`));
    await expect(viewer.locator('[data-popover][aria-hidden="true"]'), step).toHaveCount(0);
   };
   await input.focus();
   await still('before any write');
   await agentWrites(request, agent, live, { old_string: 'line 28 with ordinary text', new_string: 'line 28 with ordinary text and a much longer rewritten sentence that wraps\n\nnew paragraph one\n\nnew paragraph two' }, 'edit_file');
   await viewer.bringToFront();
   await still('after an agent edit above');
   await agentWrites(request, agent, live, { content: body(true) }, 'write_file');
   await viewer.bringToFront();
   await still('after an agent rewrote the whole file');
   await suggest(request, live, other.id, 'line 40 reworded entirely', 'line 40 suggested', 'A suggestion');
   const reply = await request.post('/api/delegation/docs/comments', { params: { token: TEST_TOKEN }, data: { parent_id: root, body: 'Remote reply', as_session_id: other.id } });
   expect(reply.ok(), await reply.text()).toBeTruthy();
   await viewer.waitForTimeout(1500);
   await still('after a suggestion and a reply from another thread');
   const probe = await viewer.evaluate(() => (window as unknown as { __probe: object }).__probe);
   expect(probe, 'the box was never hidden or left').toEqual({ hidden: 0, blurs: 0 });
   expect(await serverText(request, live)).not.toContain('typing a comment');
   expect(await serverText(request, live)).not.toMatch(/\.{3}/);
  } finally { await context.close(); }
 });
}

const NOTE = 'A long wrapping note that keeps going so the cell wraps onto several lines at ordinary pane widths, with more words after it.';
const tableBody = (cell = 'Short note.') => [
 '# Roadmap', '',
 ...Array.from({ length: 8 }, (_, i) => `intro line ${i} with ordinary text\n`),
 '| # | Capability | Notes |', '| --- | --- | --- |',
 `| 1 | Plain words in a cell | ${NOTE} |`,
 `| 2 | Second row | ${cell} |`,
 `| 3 | Third row | Another note. |`, '',
 ...Array.from({ length: 20 }, (_, i) => `closing line ${i} with ordinary text\n`),
].join('\n');

/** Page points just inside the first and last characters of `text` in the cell. */
async function textEnds(page: Page, cellText: string, text: string) {
 return page.evaluate(({ cellText, text }) => {
  const cell = [...document.querySelectorAll<HTMLElement>('.cm-md-table td')].find((td) => td.textContent?.includes(cellText))!;
  const start = cell.textContent!.indexOf(text);
  const point = (offset: number) => {
   const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
   for (let seen = 0, node = walker.nextNode() as Text | null; node; seen += node.data.length, node = walker.nextNode() as Text | null) {
    if (offset < seen + node.data.length) {
     const range = document.createRange();
     range.setStart(node, offset - seen); range.setEnd(node, offset - seen + 1);
     const box = range.getBoundingClientRect();
     return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
    }
   }
   throw new Error(`offset ${offset} is outside the cell`);
  };
  return { from: point(start), to: point(start + text.length - 1) };
 }, { cellText, text });
}

test(`@comment-box-outside-write a comment box on text in a table survives an agent editing the table`, async ({ browser, request }) => {
 test.setTimeout(240_000);
 const stamp = `${Date.now()}`;
 const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
 const id = await createSession(request, `Writer ${stamp}`);
 promoted.push(id);
 try {
  const agentPage = await context.newPage();
  await loginByUrl(agentPage);
  await agentPage.locator('.thread-item').filter({ hasText: `Writer ${stamp}` }).click({ timeout: 10_000 });
  const agent = { id, title: `Writer ${stamp}`, page: agentPage };
  const live = await agentCreatesSharedDocument(request, agent, `table-${stamp}`, tableBody());
  const viewer = await context.newPage();
  await viewer.setViewportSize({ width: 1440, height: 900 });
  await openLiveDocument(viewer, live, 'intro line 0 with ordinary text');
  await viewer.bringToFront();
  await expect(viewer.locator('.cm-md-table table')).toBeVisible({ timeout: 15_000 });
  await viewer.locator('.cm-md-table').scrollIntoViewIfNeeded();
  const { from, to } = await textEnds(viewer, NOTE, 'wrapping note that keeps going so the cell wraps');
  await viewer.mouse.move(from.x, from.y); await viewer.mouse.down();
  for (let step = 1; step <= 12; step++) await viewer.mouse.move(from.x + ((to.x - from.x) * step) / 12, from.y + ((to.y - from.y) * step) / 12);
  await viewer.mouse.up();
  await viewer.locator('.sel-comment').click();
  const input = viewer.getByPlaceholder('Comment…');
  await expect(input).toBeVisible();
  await input.fill('typing a comment');
  await input.focus();
  await viewer.evaluate(() => {
   const seen = { hidden: 0, blurs: 0 };
   (window as unknown as { __probe: typeof seen }).__probe = seen;
   new MutationObserver(() => { if (document.querySelector('[data-popover][inert], [data-popover][aria-hidden="true"]')) seen.hidden++; }).observe(document.body, { attributes: true, subtree: true, childList: true });
   document.addEventListener('focusout', event => { if ((event.target as HTMLElement).matches?.('textarea')) seen.blurs++; }, true);
  });
  let typed = 0;
  const still = async (step: string) => {
   await expect(input, step).toBeVisible();
   await expect(input, step).toBeFocused();
   await input.pressSequentially('.'); typed++;
   await expect(input, step).toHaveValue(new RegExp(`^typing a comment\\.{${typed}}$`));
   await expect(viewer.locator('[data-popover][aria-hidden="true"]'), step).toHaveCount(0);
  };
  await still('before any write');
  await agentWrites(request, agent, live, { old_string: '| Short note. |', new_string: '| Short note, now reworded and longer so the row grows. |' }, 'edit_file');
  await viewer.bringToFront();
  await still('after an agent edited another cell of the same table');
  await agentWrites(request, agent, live, { old_string: 'intro line 3 with ordinary text', new_string: 'intro line 3 with ordinary text\n\nA new paragraph above\n\nand another one' }, 'edit_file');
  await viewer.bringToFront();
  await still('after an agent added paragraphs above the table');
  await viewer.locator('.cm-scroller').evaluate(e => { e.scrollTop += 30; });
  await viewer.waitForTimeout(500);
  await still('after the page moved');
  const probe = await viewer.evaluate(() => (window as unknown as { __probe: object }).__probe);
  expect(probe, 'the box was never hidden or left').toEqual({ hidden: 0, blurs: 0 });
  expect(await serverText(request, live)).not.toContain('typing a comment');
 } finally { await context.close(); }
});

/** A roadmap-sized document: many sections, each a table whose wrapped rows give the editor heights it can only estimate until drawn. */
const section = (n: number, long: boolean) => [
 `## Section ${n}`, '',
 `Lead paragraph for section ${n}, with ordinary text.`, '',
 '| # | Capability | Notes |', '| --- | --- | --- |',
 ...Array.from({ length: 10 }, (_, r) => `| ${r + 1} | Row ${n}.${r + 1} | ${long ? NOTE.repeat(3) : 'Short note.'} marker-${n}-${r} |`), '',
].join('\n');
const roadmap = (longAbove: boolean) => Array.from({ length: 14 }, (_, n) => section(n, longAbove && n < 7)).join('\n');

test(`@comment-box-outside-write a comment box in a long table document survives a whole-file rewrite that changes heights above it`, async ({ browser, request }) => {
 test.setTimeout(240_000);
 const stamp = `${Date.now()}`;
 const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
 const id = await createSession(request, `Writer ${stamp}`);
 promoted.push(id);
 try {
  const agentPage = await context.newPage();
  await loginByUrl(agentPage);
  await agentPage.locator('.thread-item').filter({ hasText: `Writer ${stamp}` }).click({ timeout: 10_000 });
  const agent = { id, title: `Writer ${stamp}`, page: agentPage };
  const live = await agentCreatesSharedDocument(request, agent, `roadmap-${stamp}`, roadmap(false));
  const viewer = await context.newPage();
  await viewer.setViewportSize({ width: 1440, height: 900 });
  await openLiveDocument(viewer, live, 'Lead paragraph for section 0');
  await viewer.bringToFront();
  await viewer.locator('.cm-scroller').evaluate(e => { e.scrollTop = e.scrollHeight * 0.62; });
  await expect(viewer.locator('.cm-md-table table').first()).toBeVisible({ timeout: 15_000 });
  await viewer.waitForTimeout(800);
  const mark = await viewer.evaluate(() => {
   const tds = [...document.querySelectorAll<HTMLElement>('.cm-md-table td')].filter(td => /marker-/.test(td.textContent ?? ''));
   const td = tds.find(t => { const r = t.getBoundingClientRect(); return r.top > 200 && r.bottom < innerHeight - 100; });
   return td ? td.textContent!.match(/marker-\d+-\d+/)![0] : null;
  });
  expect(mark, 'a table cell is on screen').not.toBeNull();
  const { from, to } = await textEnds(viewer, mark!, mark!);
  await viewer.mouse.move(from.x, from.y); await viewer.mouse.down();
  for (let step = 1; step <= 12; step++) await viewer.mouse.move(from.x + ((to.x - from.x) * step) / 12, from.y + ((to.y - from.y) * step) / 12);
  await viewer.mouse.up();
  await viewer.locator('.sel-comment').click();
  const input = viewer.getByPlaceholder('Comment…');
  await expect(input).toBeVisible();
  await input.fill('typing a comment');
  await input.focus();
  await viewer.evaluate(() => {
   const seen = { hidden: 0, blurs: 0, scrolls: 0 };
   (window as unknown as { __probe: typeof seen }).__probe = seen;
   new MutationObserver(() => { if (document.querySelector('[data-popover][inert], [data-popover][aria-hidden="true"]')) seen.hidden++; }).observe(document.body, { attributes: true, subtree: true, childList: true });
   document.addEventListener('focusout', event => { if ((event.target as HTMLElement).matches?.('textarea')) seen.blurs++; }, true);
   document.addEventListener('scroll', () => seen.scrolls++, true);
  });
  const rewritten = roadmap(true);
  await agentWrites(request, agent, live, { content: rewritten }, 'write_file');
  await viewer.bringToFront();
  await viewer.waitForTimeout(1500);
  const probe = await viewer.evaluate(() => (window as unknown as { __probe: object }).__probe);
  await expect(input).toBeVisible();
  await expect(input).toBeFocused();
  await input.pressSequentially('.');
  await expect(input).toHaveValue('typing a comment.');
  expect(probe, 'the box was never hidden or left').toMatchObject({ hidden: 0, blurs: 0 });
  expect(await serverText(request, live)).not.toContain('typing a comment');
 } finally { await context.close(); }
});
