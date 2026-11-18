import type { APIRequestContext, Locator, Page } from '@playwright/test';
import { expect, test } from '../support/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage, releaseMockGate, seedMockResponses } from '../support/mock';
import { commentEditor, commentOnMessage, openCommentThread, postedComments } from '../support/chatComments';
import { selectPhrase } from '../support/chatCommentFrames';
import { chooseThread, openSuggestionDocument } from '../support/docSuggestions';
import { widen } from '../support/marginCards';
import { DOC, imageTag, openDocument, openImage, overlay, seedImageDocument } from '../support/docImageComments';
import { apiComment, asAgent, openThread as openImageThread, seedImage } from '../support/imageRegionComments';
import { execFileSync } from 'node:child_process';
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
	return { row, thread, gate, title };
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

// ---------------------------------------------------------------- a press on a chat card that rests (not the open one)
/** The chat margin card whose note reads `note`. */
const chatCard = (page: Page, note: string) => page.locator('.chat-margin .chat-margin-card').filter({ hasText: note }).first();
const anyUnreadMark = (row: Locator) => row.locator('.cm-comment-anchor[data-unread], [data-comment-ids][data-unread]');

test('chat 1440: an answer that arrives while its card rests is read by one press on that card', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	const { row, thread, gate } = await chatWithHeldAnswer(page, request, 1440);
	await raiseChatMargin(page);
	await page.keyboard.press('Escape');
	await expect(page.locator('.chat-margin .chat-margin-card.selected')).toHaveCount(0);
	await arrive(request, thread, gate);
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(await answerUnread(request, thread), 'an answer reaching a resting card reads nothing').toBe(true);
	await chatCard(page, 'A note on the phrase').getByText('A note on the phrase').first().click();
	await expect.poll(() => answerUnread(request, thread), { timeout: 10_000 }).toBe(false);
	await page.keyboard.press('Escape');
	await expect(anyUnreadMark(row)).toHaveCount(0);
});

test('chat 1440: with one card open, a single press on another card that rests with an unread answer reads it and lets the first go', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	// The held answer is to the first note; the second note's card is the one left open.
	const { row, thread, gate } = await chatWithHeldAnswer(page, request, 1440, { second: true });
	await raiseChatMargin(page);
	const first = await openCommentThread(row, page, 'a comment');
	await expect(first).toContainText('A second note');
	await arrive(request, thread, gate);
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(await answerUnread(request, thread), 'an answer reaching a resting card reads nothing').toBe(true);
	const resting = chatCard(page, 'A note on the phrase');
	await resting.getByText('A note on the phrase').first().click();
	await expect.poll(() => answerUnread(request, thread), { timeout: 10_000 }).toBe(false);
	await expect(resting).toHaveClass(/\bselected\b/);
	await expect(chatCard(page, 'A second note')).not.toHaveClass(/\bselected\b/);
	await page.keyboard.press('Escape');
	await expect(anyUnreadMark(row)).toHaveCount(0);
});

test('chat 1440 with the sidebar and panel open: an answer that arrived before its thread opened is read by a press on its highlight', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	const { row, thread, gate } = await chatWithHeldAnswer(page, request, 1440);
	// No margin: the sidebar and the side panel leave the transcript too narrow for it, so a thread opens as a popover.
	await expect(page.locator('.chat-margin .chat-margin-card')).toHaveCount(0);
	await page.keyboard.press('Escape');
	await arrive(request, thread, gate);
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(await answerUnread(request, thread), 'an answer reaching a closed thread reads nothing').toBe(true);
	await openCommentThread(row, page, QUOTE);
	await expect.poll(() => answerUnread(request, thread), { timeout: 10_000 }).toBe(false);
	await page.keyboard.press('Escape');
	await expect(anyUnreadMark(row)).toHaveCount(0);
});

// ---------------------------------------------------------------- an image thread opened from the document
for (const way of ['card', 'tag'] as const) {
	test(`image in a document 1440: a thread opened by its ${way === 'card' ? 'card in the margin' : 'tag on the image'} is read, and a reply that arrives while it is open is read when the image closes`, async ({ page, request }, info) => {
		test.skip(!mocked() || !onProject(info.project.name, 1440));
		test.setTimeout(180_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		const seeded = await seedImageDocument(page, request, `read-every-path-image-${way}`, 5000);
		// An agent's top-level note counts as unread only on a document the person has already commented on.
		const own = 'Paragraph 1: the walkthrough';
		const mine = await request.post(`/api/artifacts/${seeded.artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { delivery_policy: 'immediate', body: 'A note of my own.', anchor_text: own, from_off: DOC.indexOf(own), to_off: DOC.indexOf(own) + own.length } });
		expect(mine.ok(), await mine.text()).toBeTruthy();
		const markdown = '![Mock screen](shots/screen.png)';
		const from = DOC.indexOf(markdown);
		const created = await request.post(`/api/artifacts/${seeded.artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { delivery_policy: 'immediate', body: 'An agent note on the image.', region: { x: 0.2, y: 0.25, w: 0.25, h: 0.25 }, anchor_text: markdown, from_off: from, to_off: from + markdown.length } });
		expect(created.ok(), await created.text()).toBeTruthy();
		const id = (await created.json()).comment.id as string;
		asAgent(seeded, id);
		expect(await threadUnread(request, seeded.artifactId, id), 'setup: the agent note is unread').toBe(true);
		const stamps = watchStamps(page);
		await openDocument(page, seeded, 1440);
		await page.waitForTimeout(SEEN_FOR_MS);
		expect(stamps(), 'opening the document reads nothing').toEqual([]);
		if (way === 'card') {
			await widen(page, { artifactId: seeded.artifactId, ids: [id] } as never);
			const card = page.locator(`[data-margin-card="${id}"]`);
			await card.scrollIntoViewIfNeeded();
			await page.waitForTimeout(SEEN_FOR_MS);
			expect(stamps(), 'showing the card reads nothing').toEqual([]);
			expect(await threadUnread(request, seeded.artifactId, id), 'showing the card reads nothing').toBe(true);
			await card.getByText('An agent note on the image.').first().click();
			await expect(overlay(page)).toBeVisible();
		} else {
			await openImage(page);
			await page.waitForTimeout(SEEN_FOR_MS);
			expect(stamps(), 'opening the image reads nothing').toEqual([]);
			expect(await threadUnread(request, seeded.artifactId, id), 'opening the image reads nothing').toBe(true);
			await imageTag(page, 1).click();
		}
		await expect.poll(() => threadUnread(request, seeded.artifactId, id), { timeout: 10_000 }).toBe(false);
		// The reply must post while the thread is open; if it waited in the writer line, the read on close below would test nothing.
		expect(await agentDocReply(request, id, seeded.sessionId, 'A later answer.'), 'the reply posts while the image thread is open').not.toBeNull();
		await expect.poll(() => threadUnread(request, seeded.artifactId, id)).toBe(true);
		await page.waitForTimeout(SEEN_FOR_MS);
		expect(await threadUnread(request, seeded.artifactId, id), 'arriving while open reads nothing yet').toBe(true);
		for (let i = 0; i < 3 && await overlay(page).isVisible(); i += 1) { await page.keyboard.press('Escape'); await page.waitForTimeout(400); }
		await expect(overlay(page)).toBeHidden();
		await expect.poll(() => threadUnread(request, seeded.artifactId, id), { timeout: 10_000 }).toBe(false);
	});
}

// ---------------------------------------------------------------- documents with unread agent notes
const NOTE_A = 'First agent note.';
const NOTE_B = 'Second agent note.';
/** A document with an agent note on each of `notes`' lines, each with a reply to the person that waits unread. */
async function docWithUnread(page: Page, request: APIRequestContext, width: number, notes: Array<{ quote: string; body: string }>) {
	await page.setViewportSize({ width, height: width <= 768 ? 852 : 900 });
	return openSuggestionDocument(page, request, docText, notes, { splitWidth: 5000, beforeOpen: async (seeded) => {
		for (const id of seeded.ids) {
			const mine = await request.post(`/api/artifacts/${seeded.artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { body: 'Why?', parent_id: id, delivery_policy: 'immediate' } });
			expect(mine.ok(), await mine.text()).toBeTruthy();
			expect(await agentDocReply(request, id, seeded.ownerId, 'Because it reads better.')).not.toBeNull();
		}
	} });
}

/**
 * Goes to the chat titled `title` from the sidebar, opening the sidebar first when it is folded away. A folded sidebar keeps
 * its rows laid out, beside the screen or clipped to nothing, so a row counts as shown only when a press at its centre lands on it.
 */
async function goToChat(page: Page, title: string) {
	const item = page.locator('.thread-item').filter({ hasText: title }).first();
	const shown = () => item.evaluate((el) => {
		const box = el.getBoundingClientRect();
		const hit = box.width > 0 && box.height > 0 ? document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2) : null;
		return !!hit && el.contains(hit);
	}).catch(() => false);
	// An open sidebar may only have the row scrolled out of its list; bring it in before deciding the sidebar is folded.
	if (!(await shown())) await item.evaluate((el) => el.scrollIntoView({ block: 'nearest' })).catch(() => undefined);
	if (!(await shown())) await page.getByRole('button', { name: /^Toggle sidebar/ }).click();
	await expect.poll(shown, { timeout: 10_000 }).toBe(true);
	await item.click();
}

// ---------------------------------------------------------------- a list thread picked away from is let go
for (const width of [900, 393] as const) {
	test(`document ${width}: a thread picked away from in the list is let go, so a reply that reaches it later stays unread`, async ({ page, request }, info) => {
		test.skip(!mocked() || !onProject(info.project.name, width));
		test.setTimeout(180_000);
		const doc = await docWithUnread(page, request, width, [{ quote: docQuote, body: NOTE_A }, { quote: 'document line 26', body: NOTE_B }]);
		const [a, b] = doc.ids;
		const row = (note: string) => page.locator('[data-comment-card]').filter({ hasText: note }).first();
		await page.getByRole('button', { name: /^\d+ comments?\b/ }).first().click();
		await row(NOTE_A).getByText(NOTE_A).first().click();
		await expect.poll(() => threadUnread(request, doc.artifactId, a)).toBe(false);
		const back = page.getByRole('button', { name: /^(All threads|Back)/ }).first();
		if (await back.isVisible().catch(() => false)) await back.click();
		await row(NOTE_B).getByText(NOTE_B).first().click();
		await expect.poll(() => threadUnread(request, doc.artifactId, b)).toBe(false);
		expect(await agentDocReply(request, a, doc.ownerId, 'A later answer to the first note.'), 'a reply to the thread let go posts at once').not.toBeNull();
		await expect.poll(() => threadUnread(request, doc.artifactId, a)).toBe(true);
		if (width <= 768) await page.getByRole('dialog').getByRole('button', { name: /^Close/ }).first().click();
		else await page.keyboard.press('Escape');
		await page.waitForTimeout(SEEN_FOR_MS);
		expect(await threadUnread(request, doc.artifactId, a), 'closing the list does not read a thread let go before its reply came').toBe(true);
	});
}

// ---------------------------------------------------------------- loading guards: nothing reads without the person engaging
test('document 1440: loading, scrolling past, switching chats and back, and reloading read nothing', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	const stamps = watchStamps(page);
	const doc = await docWithUnread(page, request, 1440, [{ quote: docQuote, body: NOTE_A }]);
	await widen(page, doc);
	const id = doc.ids[0];
	const card = page.locator(`[data-margin-card="${id}"]`);
	await bringLineIntoView(page, docQuote);
	await expect(card).toBeVisible();
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(await threadUnread(request, doc.artifactId, id), 'loading reads nothing').toBe(true);
	const scroller = (await page.locator('.cm-scroller').boundingBox())!;
	await page.mouse.move(scroller.x + scroller.width / 3, scroller.y + scroller.height / 2);
	for (const dy of [800, 800, 800, -800, -800, -800, -800, -800, -800]) { await page.mouse.wheel(0, dy); await page.waitForTimeout(150); }
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(await threadUnread(request, doc.artifactId, id), 'scrolling past reads nothing').toBe(true);
	await goToChat(page, doc.reviewerTitle);
	await page.waitForTimeout(1_000);
	await goToChat(page, 'suggestions-owner-');
	await expect(page.locator('.cm-content')).toContainText('document line', { timeout: 20_000 });
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(await threadUnread(request, doc.artifactId, id), 'switching chats and back reads nothing').toBe(true);
	// A reload lands on the workspace home; going back to the chat restores its document tab.
	await page.reload();
	await goToChat(page, 'suggestions-owner-');
	await expect(page.locator('.cm-content')).toContainText('document line', { timeout: 20_000 });
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(stamps(), 'no read was sent').toEqual([]);
	expect(await threadUnread(request, doc.artifactId, id), 'reloading reads nothing').toBe(true);
});

test('chat 1440: loading, scrolling past, switching chats and back, and reloading read nothing', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	const otherTitle = `Read every path other ${crypto.randomUUID()}`;
	await createSession(request, otherTitle);
	const stamps = watchStamps(page);
	const { thread, gate, title } = await chatWithHeldAnswer(page, request, 1440);
	await raiseChatMargin(page);
	await page.keyboard.press('Escape');
	await arrive(request, thread, gate);
	// A reload lands on the workspace home; the chat loads when the person goes back to it.
	await page.reload();
	await goToChat(page, title);
	await waitForWsConnected(page);
	await expect(page.locator('.cm-comment-anchor').filter({ hasText: QUOTE })).toBeVisible();
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(await answerUnread(request, thread), 'loading reads nothing').toBe(true);
	const transcript = (await page.locator('.messages').boundingBox())!;
	await page.mouse.move(transcript.x + transcript.width / 2, transcript.y + transcript.height / 2);
	for (const dy of [-800, -800, -800, 800, 800, 800, 800]) { await page.mouse.wheel(0, dy); await page.waitForTimeout(150); }
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(await answerUnread(request, thread), 'scrolling past reads nothing').toBe(true);
	await goToChat(page, otherTitle);
	await page.waitForTimeout(1_000);
	await goToChat(page, title);
	await expect(page.locator('.cm-comment-anchor').filter({ hasText: QUOTE })).toBeVisible();
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(stamps(), 'no read was sent').toEqual([]);
	expect(await answerUnread(request, thread), 'switching chats and back reads nothing').toBe(true);
});

// ---------------------------------------------------------------- the jump control
test('document 1440: the next-unread control reads the thread it lands on, and lets its New go on Escape', async ({ page, request }, info) => {
	test.skip(!mocked() || !onProject(info.project.name, 1440));
	test.setTimeout(180_000);
	const doc = await docWithUnread(page, request, 1440, [{ quote: docQuote, body: NOTE_A }]);
	await widen(page, doc);
	const id = doc.ids[0];
	// Scrolled to the top by hand (a wheel), after the margin's opening ease, so the unread thread is off screen below.
	await bringLineIntoView(page, 'document line 0');
	const scroller = (await page.locator('.cm-scroller').boundingBox())!;
	await page.mouse.move(scroller.x + scroller.width / 3, scroller.y + scroller.height / 2);
	await page.mouse.wheel(0, -4000);
	await expect.poll(() => page.locator('.cm-scroller').evaluate((el) => el.scrollTop)).toBe(0);
	const stamps = watchStamps(page);
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(stamps(), 'nothing reads before the press').toEqual([]);
	const jump = page.getByRole('button', { name: /new, next/ }).first();
	await expect(jump).toBeVisible();
	await jump.click();
	await expect.poll(() => threadUnread(request, doc.artifactId, id), { timeout: 10_000 }).toBe(false);
	const card = page.locator(`[data-thread-id="${id}"]`);
	await expect(card).toHaveClass(/\bfocused\b/);
	await expect(card.locator('.ct-new'), 'New holds while the thread is open').not.toHaveCount(0);
	await page.keyboard.press('Escape');
	await expect(card.locator('.ct-new'), 'New goes once the thread is let go').toHaveCount(0);
});

// ---------------------------------------------------------------- one engagement clears every count of it together
const TASK_TEXT = 'Launch plan details for review. The rollout waits on sign-off.';
for (const width of [1440, 393] as const) {
	test(`document ${width}: one press on its only unread comment clears every count of it together`, async ({ page, request }, info) => {
		test.skip(!mocked() || !onProject(info.project.name, width));
		test.setTimeout(240_000);
		await page.setViewportSize({ width, height: 900 });
		const workspace = process.env.CORSIA_E2E_WORKSPACE;
		const state = process.env.CORSIA_E2E_STATE_DIR;
		const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
		if (!workspace || !state || !python) throw new Error('Requires disposable mock environment');
		const narrow = width <= 768;
		const title = `Read every path counts ${crypto.randomUUID().slice(0, 8)}`;
		const threadId = await createSession(request, title);
		const folder = path.join(workspace, `read-every-path-counts-${width}`);
		fs.mkdirSync(folder, { recursive: true });
		const saved = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: path.join(folder, 'Launch plan.md'), text: TASK_TEXT, thread_id: threadId, create: true } });
		expect(saved.ok(), await saved.text()).toBeTruthy();
		const artifactId = (await saved.json()).artifact.id as string;
		// One agent comment tagging the person, on its own highlight: the document's comment task counts it.
		execFileSync(python, ['frontend/e2e/support/person-comment-task-fixture.py', 'highlight', artifactId, threadId], {
			cwd: path.resolve(process.cwd(), '..'), encoding: 'utf8',
			env: { ...process.env, PYTHONPATH: 'backend:backend/tests', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
		});
		const unread = async () => ((await (await request.get(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN } })).json()).comments as any[])
			.some((comment) => !!comment.new_comment || (comment.new_reply_ids ?? []).length > 0);
		const toggleDot = page.getByTestId('menu-needs-you-dot');
		const sidebarRow = page.locator('aside.sidebar').getByRole('listitem').filter({ hasText: title });
		const plan = page.getByRole('list', { name: 'Plan steps' });
		const taskRow = plan.getByRole('listitem').filter({ hasText: 'Launch plan.md' }).filter({ has: page.locator('.count-text') }).first();
		const bar = page.locator('.review-bar');
		const unreadPill = page.getByRole('button', { name: /\d+ comments?, \d+ unread/ }).first();

		// Every count shows it: the workspace (the phone's menu toggle) or the chat's row, the plan's task row, and the document.
		await openAppPastLanding(page);
		if (narrow) {
			await expect(toggleDot).toBeVisible();
			await page.getByRole('button', { name: 'Toggle sidebar · Needs you', exact: true }).click();
		}
		await expect(sidebarRow.getByRole('button', { name: /Needs you/ })).toBeVisible();
		await sidebarThread(page, title).click();
		if (narrow) {
			const chat = page.getByRole('button', { name: 'Chat', exact: true });
			await expect(async () => {
				if (await chat.isVisible()) await chat.click({ timeout: 2000 });
				await page.getByRole('button', { name: /^Open plan —/ }).click({ timeout: 2000 });
				await expect(page.getByRole('dialog', { name: /^Plan —/ })).toBeVisible({ timeout: 2000 });
			}).toPass({ timeout: 30_000 });
		}
		await expect(taskRow.locator('.count-text')).toHaveText('1 comment');
		// The task row opens the document: a load, which reads nothing.
		await taskRow.getByRole('button').first().click();
		if (narrow) await page.getByRole('button', { name: 'Done', exact: true }).click();
		await expect(page.locator('.cm-content')).toContainText('Launch plan details');
		await expect(bar).toContainText('1 comment to review');
		await expect(unreadPill).toBeVisible();
		await page.waitForTimeout(SEEN_FOR_MS);
		expect(await unread(), 'opening the document from its task reads nothing').toBe(true);

		// One press on the comment's card.
		await unreadPill.click();
		const card = page.getByRole('region', { name: /^Comment by / }).filter({ hasText: 'Which details?' }).first();
		await expect(card.locator('.ct-new')).not.toHaveCount(0);
		await card.getByText('Which details?').first().click();
		await expect.poll(unread, { timeout: 10_000 }).toBe(false);
		if (narrow) {
			await page.getByRole('dialog').getByRole('button', { name: /^Close/ }).first().click();
			await expect(page.getByRole('dialog')).toHaveCount(0);
		} else await page.keyboard.press('Escape');

		// Every count lets it go together.
		await expect(bar).toHaveCount(0);
		await expect(unreadPill).toHaveCount(0);
		if (!(await card.isVisible())) await page.getByRole('button', { name: /^1 comment\b/ }).first().click();
		await expect(card).toBeVisible();
		await expect(card.locator('.ct-new'), 'the card’s New is gone').toHaveCount(0);
		if (narrow) {
			const close = page.getByRole('dialog').getByRole('button', { name: /^Close/ }).first();
			if (await close.isVisible()) await close.click();
			await expect(page.getByRole('dialog')).toHaveCount(0);
			await expect(page.getByRole('button', { name: 'Toggle sidebar · Needs you', exact: true })).toHaveCount(0);
			// A fresh load shows the menu toggle without the workspace's needs-you dot.
			await openAppPastLanding(page);
			await expect(page.getByRole('button', { name: 'Toggle sidebar', exact: true })).toBeVisible();
			await expect(toggleDot, 'the workspace’s needs-you dot is gone').toHaveCount(0);
		} else {
			await page.keyboard.press('Escape');
			await expect(taskRow, 'the task row no longer counts it').toHaveCount(0);
			await expect(sidebarRow.getByRole('button', { name: /Needs you/ }), 'the chat’s row no longer needs the person').toHaveCount(0);
		}
	});
}
