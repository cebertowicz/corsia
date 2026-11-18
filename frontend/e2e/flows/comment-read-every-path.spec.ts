import { expect, test, type APIRequestContext, type Locator, type Page } from '../support/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage, releaseMockGate, seedMockResponses } from '../support/mock';
import { commentEditor, commentOnMessage, openCommentThread, postedComments } from '../support/chatComments';
import { selectPhrase } from '../support/chatCommentFrames';
import { chooseThread, openSuggestionDocument } from '../support/docSuggestions';
import { widen } from '../support/marginCards';
import { DOC, openDocument, openImage, seedImageDocument } from '../support/docImageComments';
import { apiComment, asAgent, openThread as openImageThread, seedImage } from '../support/imageRegionComments';
import fs from 'node:fs';
import path from 'node:path';

/**
 * A thread the person engaged with is read on every way they leave it, so a reply that arrived while it stayed open is read
 * when they let it go. Engaging is one call for every way in (a press on its card, its highlight, its number disc, focus in
 * its reply field). A press on a link in the card, or a press that ends a text selection, is not engaging and reads nothing.
 * A thread nobody engaged with reads nothing when it closes.
 */

test.use({ seedPerTest: true });

/** Well past any timer the comment surfaces run, so a read that should not happen would have been seen. */
const SEEN_FOR_MS = 3_000;
const QUOTE = 'selected phrase';
const LINK_HOST = ['https:', '', 'example.com'].join('/');
const messagesUrl = (thread: string) => `/api/threads/${thread}/messages?token=${encodeURIComponent(TEST_TOKEN)}`;
const mocked = () => process.env.CORSIA_E2E_MOCK === '1';
const onProject = (name: string, width: number) => name.endsWith(width <= 768 ? 'mobile' : 'desktop');

function watchStamps(page: Page): () => string[] {
	const urls: string[] = [];
	page.on('request', (request) => {
		if (request.method() === 'POST' && /\/comments\/[^/]+\/viewed/.test(request.url())) urls.push(request.url());
	});
	return () => [...urls];
}

/** Whether the chat's agent answer is unread on the server; null until it has arrived. */
async function answerUnread(request: APIRequestContext, thread: string): Promise<boolean | null> {
	const body = await (await request.get(messagesUrl(thread))).json();
	const answers = body.messages.flatMap((row: any) => (row.chat_comments ?? []).flatMap((comment: any) => comment.answers ?? []))
		.filter((answer: any) => answer.author_kind === 'agent');
	return answers.length ? answers.some((answer: any) => answer.unread) : null;
}

async function openChat(page: Page, title: string) {
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, title).click();
	await waitForWsConnected(page);
}

/** A chat with one comment (two with `second`) whose agent answer is held at the returned gate. */
async function chatWithHeldAnswer(page: Page, request: APIRequestContext, width: number, opts: { note?: string; second?: boolean } = {}) {
	await page.setViewportSize({ width, height: width <= 768 ? 852 : 900 });
	const title = `Read every path ${width} ${crypto.randomUUID()}`;
	const thread = await createSession(request, title);
	await injectMessage(request, thread, { role: 'assistant', content: 'Earlier context.\n\n'.repeat(24), metadata: { for_user: true } }, TEST_TOKEN);
	const id = await injectMessage(request, thread, { role: 'assistant', content: 'A selected phrase for a comment.', metadata: { for_user: true } }, TEST_TOKEN);
	const gate = `read-every-path-${crypto.randomUUID()}`;
	await seedMockResponses(request, [
		{ content: '', response_gate: gate, tool_calls: [{ id: 'update-1', name: 'post_update', arguments: JSON.stringify({ text: '[c2.1] The answer to read.' }) }], finish_reason: 'tool_calls' },
		{ content: 'Done.' },
	]);
	await openAppPastLanding(page);
	await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
	await openChat(page, title);
	const row = page.locator(`[data-message-id="${id}"]`);
	await expect(row).toBeVisible();
	await selectPhrase(page, id, QUOTE);
	await commentEditor(page).fill(opts.note ?? 'A note on the phrase');
	await page.getByRole('dialog', { name: 'New comment' }).getByRole('button', { name: 'Comment', exact: true }).click();
	await commentOnMessage(row, QUOTE);
	await expect.poll(async () => postedComments((await (await request.get(messagesUrl(thread))).json()).messages).map((c) => c.cid)).toEqual(['c2.1']);
	if (opts.second) {
		if (width <= 768) await page.keyboard.press('Escape');
		await selectPhrase(page, id, 'a comment');
		await commentEditor(page).fill('A second note');
		await page.getByRole('dialog', { name: 'New comment' }).getByRole('button', { name: 'Comment', exact: true }).click();
		await commentOnMessage(row, 'a comment');
		await expect.poll(async () => postedComments((await (await request.get(messagesUrl(thread))).json()).messages).length).toBe(2);
	}
	return { row, thread, gate };
}

async function arrive(request: APIRequestContext, thread: string, gate: string) {
	await releaseMockGate(request, gate);
	await expect.poll(() => answerUnread(request, thread), { timeout: 30_000 }).toBe(true);
}

/** The margin column fits at 1440 once the sidebar and the side panel are closed. */
async function raiseChatMargin(page: Page) {
	await page.getByRole('button', { name: /^Toggle sidebar/ }).click();
	await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
	await expect(page.locator('.chat-margin .chat-margin-card').first()).toBeVisible();
}

/** A press on the transcript away from any highlight or card. */
async function pressOutside(page: Page) {
	const box = (await page.locator('.messages').boundingBox())!;
	await page.mouse.click(box.x + 40, box.y + box.height / 3);
}

// ---------------------------------------------------------------- a reply that arrives while the thread is open
const exits = [
	{ width: 1440, exit: 'Escape' }, { width: 1440, exit: 'a press outside' }, { width: 1440, exit: 'picking another thread' },
	{ width: 900, exit: 'Escape' }, { width: 900, exit: 'a press outside' }, { width: 900, exit: 'picking another thread' },
	{ width: 393, exit: 'its Close button' },
] as const;
for (const { width, exit } of exits) {
	test(`chat ${width}: an answer that arrives while its thread is open is read when the person leaves by ${exit}`, async ({ page, request }, info) => {
		test.skip(!mocked() || !onProject(info.project.name, width));
		test.setTimeout(180_000);
		const { row, thread, gate } = await chatWithHeldAnswer(page, request, width, { second: exit === 'picking another thread' });
		if (width === 1440) await raiseChatMargin(page);
		const opened: Locator = await openCommentThread(row, page, QUOTE);
		await arrive(request, thread, gate);
		await page.waitForTimeout(SEEN_FOR_MS);
		expect(await answerUnread(request, thread), 'arriving while open reads nothing yet').toBe(true);
		if (exit === 'Escape') await page.keyboard.press('Escape');
		else if (exit === 'a press outside') await pressOutside(page);
		else if (exit === 'its Close button') await opened.getByRole('button', { name: 'Close', exact: true }).click();
		else {
			if (width !== 1440) await page.keyboard.press('Escape');
			await row.locator('[data-comment-ids]').filter({ hasText: 'a comment' }).first().click();
		}
		await expect.poll(() => answerUnread(request, thread), { timeout: 10_000 }).toBe(false);
		// The page shows it read too, not only the server.
		await expect(row.locator('.cm-comment-anchor[data-unread], [data-comment-ids][data-unread]')).toHaveCount(0);
		if (width === 1440 && exit === 'a press outside') await expect(page.locator('.chat-margin .chat-margin-card.selected')).toHaveCount(0);
	});
}

test('chat 1440: a thread nobody engaged with reads nothing when it leaves the screen', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	const otherTitle = `Read every path other ${crypto.randomUUID()}`;
	await createSession(request, otherTitle);
	const { thread, gate } = await chatWithHeldAnswer(page, request, 1440);
	await raiseChatMargin(page);
	await arrive(request, thread, gate);
	const stamps = watchStamps(page);
	await page.keyboard.press('Escape');
	await page.getByRole('button', { name: /^Toggle sidebar/ }).click();
	await sidebarThread(page, otherTitle).click();
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(stamps(), 'leaving a resting card reads nothing').toEqual([]);
	expect(await answerUnread(request, thread)).toBe(true);
});

// ---------------------------------------------------------------- presses that are not engaging
for (const how of ['link', 'selection'] as const) {
	test(`chat 1440: a ${how === 'link' ? 'press on a link in the card' : 'press that ends a text selection in the card'} reads nothing`, async ({ page, request }, info) => {
		test.skip(!mocked() || !onProject(info.project.name, 1440));
		test.setTimeout(180_000);
		await page.context().route(`${LINK_HOST}/**`, (route) => route.fulfill({ status: 200, body: 'ok' }));
		const { thread, gate } = await chatWithHeldAnswer(page, request, 1440, { note: `A note on the phrase, see [the guide](${LINK_HOST}/guide) first.` });
		await arrive(request, thread, gate);
		await raiseChatMargin(page);
		const card = page.locator('.chat-margin .chat-margin-card').first();
		const stamps = watchStamps(page);
		if (how === 'link') await card.getByRole('link', { name: 'the guide' }).click();
		else {
			const box = (await card.getByText('A note on the phrase').first().boundingBox())!;
			await page.mouse.move(box.x + 2, box.y + box.height / 2);
			await page.mouse.down();
			await page.mouse.move(box.x + box.width - 2, box.y + box.height / 2, { steps: 6 });
			await page.mouse.up();
		}
		await page.waitForTimeout(SEEN_FOR_MS);
		expect(stamps(), `a ${how} press reads nothing`).toEqual([]);
		expect(await answerUnread(request, thread)).toBe(true);
		// The same card still reads on a plain press, so the press above reached it.
		await page.evaluate(() => window.getSelection()?.removeAllRanges());
		await card.getByText('A note on the phrase').first().click();
		await expect.poll(() => answerUnread(request, thread)).toBe(false);
	});
}

const docQuote = 'target line for an unread comment';
const docText = Array.from({ length: 40 }, (_, i) => i === 20 ? docQuote : `document line ${i}`).join('\n\n');

/**
 * Scrolls the editor until the line holding `text` is drawn (the editor draws only the lines near the screen) and sits a third of the way down.
 * It first waits out the margin column's opening ease: the text narrows every frame until it ends, and the reading-place keeper
 * takes a scroll made by no hand meanwhile for the editor's own rewrap scroll and lays the reader's earlier place back.
 */
async function bringLineIntoView(page: Page, text: string) {
	let width = -1;
	await expect.poll(async () => {
		const now = await page.locator('.cm-content').evaluate((content) => content.clientWidth);
		const same = now === width;
		width = now;
		return same;
	}, { intervals: [400] }).toBe(true);
	await expect.poll(() => page.locator('.cm-scroller').evaluate((scroller, text) => {
		const line = [...document.querySelectorAll<HTMLElement>('.cm-line')].find((node) => node.textContent?.includes(text));
		if (!line) { scroller.scrollTop += scroller.clientHeight / 2; return false; }
		scroller.scrollTop += line.getBoundingClientRect().top - (scroller.getBoundingClientRect().top + scroller.clientHeight / 3);
		return true;
	}, text)).toBe(true);
}

/** Whether a document thread has anything unread on the server: a new comment or a new reply. */
async function threadUnread(request: APIRequestContext, artifactId: string, id: string): Promise<boolean> {
	const body = await (await request.get(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN } })).json();
	const comment = body.comments.find((item: any) => item.id === id);
	return !!comment && (!!comment.new_comment || (comment.new_reply_ids ?? []).length > 0);
}

test('document margin 1440: a press on a link in a card reads nothing', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	await page.context().route(`${LINK_HOST}/**`, (route) => route.fulfill({ status: 200, body: 'ok' }));
	await page.setViewportSize({ width: 1440, height: 900 });
	const doc = await openSuggestionDocument(page, request, docText, [
		{ quote: docQuote, body: `See [the guide](${LINK_HOST}/guide) and a few more words.` },
	], { splitWidth: 5000, beforeOpen: async (seeded) => {
		const params = { token: TEST_TOKEN };
		const mine = await request.post(`/api/artifacts/${seeded.artifactId}/comments`, { params, data: { body: 'Why?', parent_id: seeded.ids[0], delivery_policy: 'immediate' } });
		expect(mine.ok(), await mine.text()).toBeTruthy();
		const reply = await request.post('/api/delegation/docs/comments', { params, data: { parent_id: seeded.ids[0], body: 'Because it reads better.', as_session_id: seeded.ownerId } });
		expect(reply.ok(), await reply.text()).toBeTruthy();
	} });
	await widen(page, doc);
	const id = doc.ids[0];
	expect(await threadUnread(request, doc.artifactId, id), 'setup: the agent reply is unread').toBe(true);
	const card = page.locator(`[data-margin-card="${id}"]`);
	await card.scrollIntoViewIfNeeded();
	const stamps = watchStamps(page);
	await card.getByRole('link', { name: 'the guide' }).click();
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(stamps(), 'a link press reads nothing').toEqual([]);
	expect(await threadUnread(request, doc.artifactId, id)).toBe(true);
});

// ---------------------------------------------------------------- the number disc on an image comment
test('image in a document 1440: pressing a comment’s number disc in the list reads it', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	const seeded = await seedImageDocument(page, request, 'read-every-path-disc', null);
	const params = { token: TEST_TOKEN };
	const own = 'Paragraph 1: the walkthrough';
	const mine = await request.post(`/api/artifacts/${seeded.artifactId}/comments`, { params, data: { delivery_policy: 'immediate', body: 'A note of my own.', anchor_text: own, from_off: DOC.indexOf(own), to_off: DOC.indexOf(own) + own.length } });
	expect(mine.ok(), await mine.text()).toBeTruthy();
	const markdown = '![Mock screen](shots/screen.png)';
	const from = DOC.indexOf(markdown);
	const created = await request.post(`/api/artifacts/${seeded.artifactId}/comments`, { params, data: { delivery_policy: 'immediate', body: 'An agent note on the image.', region: { x: 0.2, y: 0.25, w: 0.25, h: 0.25 }, anchor_text: markdown, from_off: from, to_off: from + markdown.length } });
	expect(created.ok(), await created.text()).toBeTruthy();
	const id = (await created.json()).comment.id as string;
	asAgent(seeded, id);
	await openDocument(page, seeded, 1440);
	await openImage(page);
	await page.locator('.image-overlay').getByRole('button', { name: /^\d+ comments?$/ }).first().click();
	const card = page.locator('[data-comment-card]').filter({ hasText: 'An agent note on the image.' }).first();
	await expect(card).toBeVisible();
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(await threadUnread(request, seeded.artifactId, id), 'opening the list reads nothing').toBe(true);
	await card.locator('.ct-num').first().click();
	await expect.poll(() => threadUnread(request, seeded.artifactId, id)).toBe(false);
});

test('image file 1440: pressing a comment’s number disc in the list reads its unread reply', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	const seeded = await seedImage(page, request, 'read-every-path-file-disc', null);
	const root = await apiComment(request, seeded.artifactId, 'What is this box?', { x: 0.2, y: 0.25, w: 0.25, h: 0.25 });
	await agentDocReply(request, root, seeded.sessionId, 'It is the header.');
	expect(await threadUnread(request, seeded.artifactId, root), 'setup: the agent reply is unread').toBe(true);
	await openImageThread(page, seeded.sessionTitle, 1440);
	await page.locator('.image-viewer [data-image-comments-pill]').click();
	const card = page.locator('[data-comment-card]').filter({ hasText: 'What is this box?' }).first();
	await expect(card).toBeVisible();
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(await threadUnread(request, seeded.artifactId, root), 'opening the list reads nothing').toBe(true);
	await card.locator('.ct-num').first().click();
	await expect.poll(() => threadUnread(request, seeded.artifactId, root)).toBe(false);
});

/** An agent's reply to a document thread; null when it waits in the writer line because the person's page holds the thread. */
async function agentDocReply(request: APIRequestContext, parentId: string, agent: string, body: string): Promise<string | null> {
	const response = await request.post('/api/delegation/docs/comments', { params: { token: TEST_TOKEN }, data: { parent_id: parentId, body, as_session_id: agent } });
	expect(response.ok(), await response.text()).toBeTruthy();
	return ((await response.json()).comment?.id as string | undefined) ?? null;
}

// ---------------------------------------------------------------- hover is not a pick
test('document margin 1440: hovering a suggestion’s text rings its card and nothing else', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	let owner = '';
	const doc = await openSuggestionDocument(page, request, docText, [
		{ quote: 'document line 23', replacement: 'document line twenty-three', body: 'A suggested wording.' },
	], { splitWidth: 5000, beforeOpen: async (seeded) => {
		owner = seeded.ownerId;
		const mine = await request.post(`/api/artifacts/${seeded.artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { body: 'Why this wording?', parent_id: seeded.ids[0], delivery_policy: 'immediate' } });
		expect(mine.ok(), await mine.text()).toBeTruthy();
		expect(await agentDocReply(request, seeded.ids[0], seeded.ownerId, 'Because it reads better.')).not.toBeNull();
	} });
	await widen(page, doc);
	const id = doc.ids[0];
	const card = page.locator(`[data-thread-id="${id}"]`);
	await bringLineIntoView(page, 'document line 23');
	await page.mouse.move(2, 2);
	await expect(card).toHaveClass(/\bpreview\b/);
	// Each card's place against the suggestion's text, so scrolling the document (the hover brings the text into view) is not a move.
	const mark = page.locator('.cm-suggestion-insert').first();
	const places = () => page.evaluate(() => {
		const text = document.querySelector('.cm-suggestion-insert')!.getBoundingClientRect().top;
		return [...document.querySelectorAll('[data-margin-card]')].map((el) => Math.round(el.getBoundingClientRect().top - text));
	});
	const settled = async () => {
		let last = await places();
		await expect.poll(async () => { const now = await places(); const same = JSON.stringify(now) === JSON.stringify(last); last = now; return same; }, { intervals: [300] }).toBe(true);
		return last;
	};
	await mark.scrollIntoViewIfNeeded();
	const before = await settled();
	const stamps = watchStamps(page);
	await mark.hover();
	await page.waitForTimeout(800);
	expect(await card.getAttribute('class'), 'the hovered card stays a preview').toMatch(/\bpreview\b/);
	expect(await card.getAttribute('class'), 'a hover opens nothing').not.toMatch(/\bfocused\b/);
	expect(await places(), 'the margin does not move on hover').toEqual(before);
	await expect(card, 'the hovered text rings its card').toHaveClass(/\bringed\b/);
	// The writer turn is free: an agent's reply posts instead of waiting for the person.
	expect(await agentDocReply(request, id, owner, 'A second answer.'), 'hover holds no writer turn').not.toBeNull();
	await page.mouse.move(2, 2);
	await expect(card).not.toHaveClass(/\bringed\b/);
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(stamps(), 'hovering reads nothing').toEqual([]);
	expect(await threadUnread(request, doc.artifactId, id)).toBe(true);
});

// ---------------------------------------------------------------- a press on struck-out text, then Escape with the pointer still on it
test('document margin 1440: a suggestion opened by a press on its struck-out text lets its New go on Escape, the pointer still resting there', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	const doc = await openSuggestionDocument(page, request, docText, [
		{ quote: 'document line 23', replacement: 'document line twenty-three', body: 'A suggested wording.' },
	], { splitWidth: 5000, beforeOpen: async (seeded) => {
		const mine = await request.post(`/api/artifacts/${seeded.artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { body: 'Why this wording?', parent_id: seeded.ids[0], delivery_policy: 'immediate' } });
		expect(mine.ok(), await mine.text()).toBeTruthy();
		expect(await agentDocReply(request, seeded.ids[0], seeded.ownerId, 'Because it reads better.')).not.toBeNull();
	} });
	await widen(page, doc);
	const id = doc.ids[0];
	expect(await threadUnread(request, doc.artifactId, id), 'setup: the agent reply is unread').toBe(true);
	const card = page.locator(`[data-thread-id="${id}"]`);
	const struck = page.locator('.cm-suggestion-del').first();
	await bringLineIntoView(page, 'document line 23');
	await struck.click();
	await expect.poll(() => threadUnread(request, doc.artifactId, id)).toBe(false);
	await expect(card).toHaveClass(/\bfocused\b/);
	await expect(card.locator('.ct-new'), 'New holds while the thread is open').not.toHaveCount(0);
	await page.keyboard.press('Escape');
	// The pointer has not moved off the struck-out text: a hover rings the card, and holds nothing.
	await expect(card).not.toHaveClass(/\bfocused\b/);
	await expect(card.locator('.ct-new'), 'New goes once the thread is let go').toHaveCount(0);
	expect(await threadUnread(request, doc.artifactId, id)).toBe(false);
});

// ---------------------------------------------------------------- reading view: a reply that arrives while a card is open
const READ_ONLY_DOC = ['# Notes', '', 'Intro paragraph above the tables.', '', 'More text below it.', ''].join('\n');
test('reading view 1440: a reply that arrives while a pressed card is open is read when Escape closes it', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	await page.setViewportSize({ width: 1440, height: 1000 });
	const params = { token: TEST_TOKEN };
	const stamp = crypto.randomUUID().slice(0, 8);
	const ownerTitle = `read-every-path-owner-${stamp}`;
	const ownerId = await createSession(request, ownerTitle);
	const reviewerId = await createSession(request, `read-every-path-reviewer-${stamp}`);
	const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params })).json()).roots;
	// A file under the workspace's context folder is one the files API reports as not writable: it opens in reading view.
	const docPath = path.join(roots.find((root) => root.kind === 'workspace')!.path, 'context', `read-every-path-${stamp}.md`);
	fs.mkdirSync(path.dirname(docPath), { recursive: true });
	fs.writeFileSync(docPath, READ_ONLY_DOC);
	const registered = await request.post('/api/files/register', { params, data: { path: docPath, thread_id: ownerId } });
	expect(registered.ok(), await registered.text()).toBeTruthy();
	const artifactId = (await registered.json()).artifact.id as string;
	const created = await request.post('/api/delegation/docs/comments', { params, data: { artifact_id: artifactId, quote: 'Intro paragraph above the tables.', body: 'A note on the prose.', as_session_id: reviewerId } });
	expect(created.ok(), await created.text()).toBeTruthy();
	const id = ((await created.json()).comment ?? {}).id as string;
	// An agent's comment is unread for the person once it answers them: they reply, then the agent does.
	const mine = await request.post(`/api/artifacts/${artifactId}/comments`, { params, data: { body: 'Why?', parent_id: id, delivery_policy: 'immediate' } });
	expect(mine.ok(), await mine.text()).toBeTruthy();
	expect(await agentDocReply(request, id, ownerId, 'It reads better.')).not.toBeNull();
	await page.addInitScript(({ ownerId, docPath, artifactId }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [ownerId]: {
			tabs: [{ instanceId: 'read-every-path-tab', artifactId, path: docPath, title: docPath.split('/').pop(), loadedRevision: null, latestRevision: null, status: 'loading' }],
			activeTabId: 'read-every-path-tab', visible: true, activeSurface: 'artifact',
		} }, splitWidth: 5000 }));
	}, { ownerId, docPath, artifactId });
	await openAppPastLanding(page);
	await chooseThread(page, ownerTitle);
	await expect(page.locator('.cm-content')).toContainText('Intro paragraph', { timeout: 20_000 });
	await expect(page.locator('.cm-content')).toHaveAttribute('aria-readonly', 'true');
	await widen(page, { artifactId, ids: [id] } as never);
	expect(await threadUnread(request, artifactId, id), 'setup: the new comment is unread').toBe(true);
	const card = page.locator(`[data-margin-card="${id}"]`);
	await card.getByText('A note on the prose.').click();
	await expect.poll(() => threadUnread(request, artifactId, id)).toBe(false);
	// Reading view holds no writer turn, so the agent's reply lands while the card is open; arriving reads nothing.
	expect(await agentDocReply(request, id, ownerId, 'A later answer.'), 'the reply posts').not.toBeNull();
	await expect.poll(() => threadUnread(request, artifactId, id)).toBe(true);
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(await threadUnread(request, artifactId, id), 'arriving while open reads nothing yet').toBe(true);
	await page.keyboard.press('Escape');
	await expect.poll(() => threadUnread(request, artifactId, id), { timeout: 10_000 }).toBe(false);
});
