import { expect, test, type APIRequestContext, type Page } from '../support/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage, releaseMockGate, seedMockResponses } from '../support/mock';
import { closeCommentThread, commentEditor, commentOnMessage, openCommentThread, postedComments } from '../support/chatComments';
import { selectPhrase } from '../support/chatCommentFrames';
import { openSuggestionDocument } from '../support/docSuggestions';
import { widen } from '../support/marginCards';
import { DOC, imageTag, openDocument, openImage, seedImageDocument } from '../support/docImageComments';
import { asAgent } from '../support/imageRegionComments';

/**
 * A reply or new comment is read only when the person picks it: a click or tap on its card, its highlight, its marker or its
 * image tag, or opening its thread. Loading the page, scrolling it into view, or a caret moved into its highlight by keys reads
 * nothing. Its New mark stays while the thread is open, so the sign the reader came to see does not vanish under them.
 */

test.use({ seedPerTest: true });

/** Well past any timer the comment surfaces run, so a read sent for merely being on screen would have been seen. */
const SEEN_FOR_MS = 3_000;

/** Every read stamp the page sends: a chat answer's and a document thread's both post to `…/comments/<id>/viewed`. */
function watchStamps(page: Page): () => string[] {
	const urls: string[] = [];
	page.on('request', (request) => {
		if (request.method() === 'POST' && /\/comments\/[^/]+\/viewed/.test(request.url())) urls.push(request.url());
	});
	return () => [...urls];
}

const QUOTE = 'selected phrase';
const messagesUrl = (thread: string) => `/api/threads/${thread}/messages?token=${encodeURIComponent(TEST_TOKEN)}`;

/** Whether the chat's agent answer is still unread on the server; null until it has arrived. */
async function answerUnread(request: APIRequestContext, thread: string): Promise<boolean | null> {
	const body = await (await request.get(messagesUrl(thread))).json();
	const answers = body.messages.flatMap((row: any) => (row.chat_comments ?? []).flatMap((comment: any) => comment.answers ?? []))
		.filter((answer: any) => answer.author_kind === 'agent');
	return answers.length ? answers.some((answer: any) => answer.unread) : null;
}

/** The sidebar starts closed on a phone and open on a desktop; its closed drawer still counts as visible, so the width decides. */
async function openChat(page: Page, title: string) {
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, title).click();
	await waitForWsConnected(page);
}

/** A chat whose one comment has an agent answer waiting, unread, while its thread was never opened. */
async function chatWithUnreadAnswer(page: Page, request: APIRequestContext, width: number) {
	const narrow = width <= 768;
	await page.setViewportSize({ width, height: narrow ? 852 : 900 });
	const title = `Read on click ${width} ${crypto.randomUUID()}`;
	const thread = await createSession(request, title);
	await injectMessage(request, thread, { role: 'assistant', content: 'Earlier context.\n\n'.repeat(24), metadata: { for_user: true } }, TEST_TOKEN);
	const id = await injectMessage(request, thread, { role: 'assistant', content: 'A selected phrase for a comment.', metadata: { for_user: true } }, TEST_TOKEN);
	// A cid names the input's admission order, the same on every fresh instance; the read after posting proves it.
	const gate = `read-on-click-${width}`;
	await seedMockResponses(request, [{ content: '[c2.1] The answer to read.', response_gate: gate }]);
	await openAppPastLanding(page);
	await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
	await openChat(page, title);
	const row = page.locator(`[data-message-id="${id}"]`);
	await expect(row).toBeVisible();
	await selectPhrase(page, id, QUOTE);
	await commentEditor(page).fill('A note on the phrase');
	await page.getByRole('dialog', { name: 'New comment' }).getByRole('button', { name: 'Comment', exact: true }).click();
	await commentOnMessage(row, QUOTE);
	await expect.poll(async () => postedComments((await (await request.get(messagesUrl(thread))).json()).messages).map((comment) => comment.cid)).toEqual(['c2.1']);
	await releaseMockGate(request, gate);
	await expect.poll(() => answerUnread(request, thread), { timeout: 30_000 }).toBe(true);
	return { row, thread, title };
}

for (const width of [1440, 900, 393] as const) {
	const surface = width === 1440 ? 'margin card' : width === 900 ? 'popover' : 'sheet';
	test(`chat ${surface} at ${width}: an answer stays unread on load and on screen, and opening its thread reads it`, async ({ page, request }, info) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith(width <= 768 ? 'mobile' : 'desktop'));
		test.setTimeout(150_000);
		const { row, thread, title } = await chatWithUnreadAnswer(page, request, width);
		const stamps = watchStamps(page);
		// Load: the chat opens from scratch with the answer waiting.
		await page.reload();
		await openChat(page, title);
		if (width === 1440) {
			// The margin column fits at 1440 once the sidebar and the side panel are closed; its card shows the answer at rest.
			await page.getByRole('button', { name: /^Toggle sidebar/ }).click();
			await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
			await expect(page.locator('.chat-margin .chat-margin-card')).toContainText('The answer to read.');
		}
		await row.evaluate((el) => el.scrollIntoView({ block: 'center' }));
		await page.waitForTimeout(SEEN_FOR_MS);
		expect(stamps(), 'nothing is read on load or on screen').toEqual([]);
		expect(await answerUnread(request, thread)).toBe(true);

		const opened = await openCommentThread(row, page, QUOTE);
		await expect(opened).toContainText('The answer to read.');
		await expect.poll(() => answerUnread(request, thread)).toBe(false);
		// The New mark holds while the thread is open.
		await expect(opened.locator('.ct-new').first()).toBeVisible();
		await page.waitForTimeout(500);
		await expect(opened.locator('.ct-new').first()).toBeVisible();
		await closeCommentThread(page, opened);
		const again = await openCommentThread(row, page, QUOTE);
		await expect(again).toContainText('The answer to read.');
		await expect(again.locator('.ct-new')).toHaveCount(0);
	});
}

const docQuote = 'target line for an unread comment';
const docText = Array.from({ length: 40 }, (_, i) => i === 20 ? docQuote : `document line ${i}`).join('\n\n');

/** Whether a document comment is still unread on the server. */
async function commentUnread(request: APIRequestContext, artifactId: string, id: string): Promise<boolean> {
	const body = await (await request.get(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN } })).json();
	return !!body.comments.find((comment: any) => comment.id === id)?.new_comment;
}

/**
 * A document with one unread agent comment, open, its highlight brought into view; with `margin` the canvas is widened
 * until the margin column is up, otherwise it stays narrower than the column needs. An agent's comment counts as unread
 * once the person has commented on the document before it, so the person's own note comes first.
 */
async function documentWithUnreadComment(page: Page, request: APIRequestContext, width: number, height: number, { margin = width > 768 } = {}) {
	await page.setViewportSize({ width, height });
	const stamps = watchStamps(page);
	const wide = margin;
	const doc = await openSuggestionDocument(page, request, docText, [
		{ quote: 'document line 5', body: 'A note of my own.', author: 'person' },
		{ quote: docQuote, body: 'An agent note to read.' },
	], { splitWidth: wide ? 5000 : 540 });
	const id = doc.ids[1];
	expect(await commentUnread(request, doc.artifactId, id), 'setup: the agent comment is unread').toBe(true);
	if (wide) await widen(page, doc);
	const anchor = page.locator('.cm-comment-anchor').filter({ hasText: docQuote });
	// The editor draws only the lines near its view, so scroll down until the line is drawn, then put it a third of the way down.
	await expect.poll(() => page.locator('.cm-scroller').evaluate((scroller, quote) => {
		const line = [...document.querySelectorAll<HTMLElement>('.cm-line')].find((node) => node.textContent?.includes(quote));
		if (!line) { scroller.scrollTop += scroller.clientHeight / 2; return false; }
		scroller.scrollTop += line.getBoundingClientRect().top - (scroller.getBoundingClientRect().top + scroller.clientHeight / 3);
		return true;
	}, docQuote)).toBe(true);
	await expect(anchor).toBeVisible();
	return { id, anchor, stamps };
}

test('document margin at 1440: an unread comment stays unread on load, on screen and under a caret moved by keys, and a click on its highlight reads it', async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith('desktop'));
	test.setTimeout(150_000);
	const { id, anchor, stamps } = await documentWithUnreadComment(page, request, 1440, 900);
	const card = page.locator(`[data-margin-card="${id}"]`);
	await expect(card).toContainText('An agent note to read.');
	await expect(card.locator('.ct-new').first()).toBeVisible();
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(stamps(), 'nothing is read on load or on screen').toEqual([]);

	// The caret moved into the highlight by keys selects its card and reads nothing.
	await page.locator('.cm-line').filter({ hasText: 'document line 19' }).click();
	await page.keyboard.press('ArrowDown');
	await page.keyboard.press('ArrowDown');
	await page.waitForTimeout(1_000);
	expect(stamps(), 'a caret moved by keys reads nothing').toEqual([]);

	await anchor.click();
	await expect.poll(() => stamps().length).toBe(1);
	await expect(card.locator('.ct-new').first()).toBeVisible();
	await page.waitForTimeout(500);
	await expect(card.locator('.ct-new').first()).toBeVisible();
});

test('document sheet at 393: an unread comment stays unread on load and on screen, and a tap on its highlight reads it', async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith('mobile'));
	test.setTimeout(150_000);
	const { anchor, stamps } = await documentWithUnreadComment(page, request, 393, 852);
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(stamps(), 'nothing is read on load or on screen').toEqual([]);
	await anchor.click();
	const sheet = page.getByRole('dialog').filter({ hasText: 'An agent note to read.' });
	await expect(sheet).toBeVisible();
	await expect.poll(() => stamps().length).toBe(1);
	await expect(sheet.locator('.ct-new').first()).toBeVisible();
	await sheet.getByRole('button', { name: 'Close', exact: true }).click();
	await expect(sheet).toHaveCount(0);
	await anchor.click();
	await expect(sheet).toBeVisible();
	await expect(sheet.locator('.ct-new')).toHaveCount(0);
	expect(stamps()).toHaveLength(1);
});

test('document popover at 900: an unread comment stays unread on load and on screen, and opening it from its highlight reads it', async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith('desktop'));
	test.setTimeout(150_000);
	// The canvas is narrower than the margin column needs, so the highlight opens its thread over the text.
	const { anchor, stamps } = await documentWithUnreadComment(page, request, 900, 900, { margin: false });
	await expect(page.locator('[data-margin-card]'), 'no margin column at this width').toHaveCount(0);
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(stamps(), 'nothing is read on load or on screen').toEqual([]);
	await anchor.click();
	const popover = page.getByRole('dialog').filter({ hasText: 'An agent note to read.' });
	await expect(popover).toBeVisible();
	await expect.poll(() => stamps().length).toBe(1);
	// The New mark holds while the thread is open.
	await expect(popover.locator('.ct-new').first()).toBeVisible();
	await page.waitForTimeout(500);
	await expect(popover.locator('.ct-new').first()).toBeVisible();
	await page.keyboard.press('Escape');
	await expect(popover).toHaveCount(0);
	await anchor.click();
	await expect(popover).toBeVisible();
	await expect(popover.locator('.ct-new')).toHaveCount(0);
	expect(stamps()).toHaveLength(1);
});

test('image viewer at 1440: an unread comment on a document image stays unread until its tag is clicked', async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith('desktop'));
	test.setTimeout(150_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	const seeded = await seedImageDocument(page, request, 'read-on-click-image', null);
	// The person's own note on the text comes first, so the agent's note after it counts as unread.
	const own = 'Paragraph 1: the walkthrough';
	const mine = await request.post(`/api/artifacts/${seeded.artifactId}/comments?token=${encodeURIComponent(TEST_TOKEN)}`, {
		data: { delivery_policy: 'immediate', body: 'A note of my own.', anchor_text: own, from_off: DOC.indexOf(own), to_off: DOC.indexOf(own) + own.length },
	});
	expect(mine.ok(), await mine.text()).toBeTruthy();
	const markdown = '![Mock screen](shots/screen.png)';
	const from = DOC.indexOf(markdown);
	const created = await request.post(`/api/artifacts/${seeded.artifactId}/comments?token=${encodeURIComponent(TEST_TOKEN)}`, {
		data: { delivery_policy: 'immediate', body: 'An agent note on the image.', region: { x: 0.2, y: 0.25, w: 0.25, h: 0.25 }, anchor_text: markdown, from_off: from, to_off: from + markdown.length },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	const imageCommentId = (await created.json()).comment.id as string;
	asAgent(seeded, imageCommentId);
	expect(await commentUnread(request, seeded.artifactId, imageCommentId), 'setup: the agent comment is unread').toBe(true);
	const stamps = watchStamps(page);
	await openDocument(page, seeded, 1440);
	await openImage(page);
	await expect(imageTag(page, 1)).toBeVisible();
	await page.waitForTimeout(SEEN_FOR_MS);
	expect(stamps(), 'opening the image reads none of its comments').toEqual([]);
	await imageTag(page, 1).click();
	const card = page.locator('[data-comment-card]').filter({ hasText: 'An agent note on the image.' });
	await expect(card).toBeVisible();
	await expect.poll(() => stamps().length).toBe(1);
	// The New mark holds while its card is open.
	await page.waitForTimeout(500);
	await expect(card.locator('.ct-new').first()).toBeVisible();
});

