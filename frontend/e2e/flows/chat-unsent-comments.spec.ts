import fs from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page, type Route } from '@playwright/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage } from '../support/mock';
import { commentEditor } from '../support/chatComments';
import { selectPhrase } from '../support/chatCommentFrames';

// A chat comment pressed on that did not post comes back in place when its chat opens again, and goes on its own, once.
// Frames are written only when `CORSIA_UNSENT_FRAMES` names a folder.

const DIR = process.env.CORSIA_UNSENT_FRAMES;
const UPPER_QUOTE = 'sign in at the table by the gate';
const UPPER = `Saturday is set. Everyone should ${UPPER_QUOTE} before picking up tools, and the crew leads will split people into three groups.`;
const LOWER_QUOTE = 'Lunch is by the creek';
const LOWER = `${LOWER_QUOTE} at noon. If it rains, we meet at the community room instead and the walk moves to Sunday.`;

/** The comment box only: the chat input bar is also `div.composer` (role textbox). */
const commentBox = (page: Page) => page.locator('div.composer:not([role="textbox"])');
const submit = (page: Page) => page.getByRole('dialog', { name: 'New comment' }).locator('button.submit');

async function shot(page: Page, name: string) {
	if (!DIR) return;
	fs.mkdirSync(DIR, { recursive: true });
	await page.emulateMedia({ colorScheme: 'light' });
	await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
	await page.waitForTimeout(250);
	await page.screenshot({ path: path.join(DIR, `${name}-${page.viewportSize()!.width}-light.png`), animations: 'disabled' });
}

type Chat = { thread: string; title: string; ids: string[] };

async function seedChat(request: APIRequestContext, title: string, bodies: string[]): Promise<Chat> {
	const thread = await createSession(request, title);
	const ids: string[] = [];
	for (const content of bodies) ids.push(await injectMessage(request, thread, { role: 'assistant', content, metadata: { for_user: true } }, TEST_TOKEN));
	return { thread, title, ids };
}

async function openChat(page: Page, chat: Chat) {
	await openAppPastLanding(page);
	if (page.viewportSize()!.width < 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, chat.title).click();
	await waitForWsConnected(page);
	for (const id of chat.ids) await expect(page.locator(`[data-message-id="${id}"]`)).toBeVisible();
}

const commentsRoute = (chat: Chat) => `**/api/threads/${chat.thread}/chat-comments**`;

/**
 * Every post to the chat's comments fails as if the connection were down. Going back online leaves the page first, so a
 * retry from before is never counted as one from the chat opened again.
 */
async function offline(page: Page, chat: Chat): Promise<() => Promise<void>> {
	const drop = (route: Route) => route.request().method() === 'POST' ? route.abort('internetdisconnected') : route.fallback();
	await page.route(commentsRoute(chat), drop);
	return async () => { await page.goto('about:blank'); await page.unroute(commentsRoute(chat), drop); };
}

/**
 * Posts wait until released; each one let through is recorded with its client id. With `quoteGone`, the server's answer
 * to each is its refusal for a quote that is not in the chat, and nothing is stored.
 */
async function held(page: Page, chat: Chat, quoteGone = false) {
	let release = () => {};
	const gate = new Promise<void>((resolve) => { release = resolve; });
	const through: string[] = [], refused: string[] = [];
	await page.route(commentsRoute(chat), async (route) => {
		if (route.request().method() !== 'POST') return route.fallback();
		await gate;
		const id = (route.request().postDataJSON() as { input_id: string }).input_id;
		if (!quoteGone) { through.push(id); return route.fallback(); }
		refused.push(id);
		await route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ detail: 'comment_quote_gone' }) });
	});
	return { release: () => release(), through, refused };
}

/** Select `quote`, type `note` and press Comment, with no connection: the box reads Sending… and keeps the words. */
async function pressOffline(page: Page, messageId: string, quote: string, note: string) {
	await selectPhrase(page, messageId, quote);
	await commentEditor(page).fill(note);
	await submit(page).click();
	await expect(submit(page)).toHaveText('Sending…', { timeout: 15_000 });
	await expect(commentEditor(page)).toHaveValue(note);
}

/** The chat's comment highlights on `quote`, once posted. */
const placed = (page: Page, messageId: string, quote: string) =>
	page.locator(`[data-message-id="${messageId}"] [data-comment-ids]`, { hasText: quote });

test.describe('a pressed chat comment that did not post comes back in place', () => {
	for (const width of [393, 1440] as const) {
		test(`it reopens on its passage reading Sending…, and posts once ${width}`, async ({ page, request }) => {
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			const chat = await seedChat(request, width === 393 ? 'Crew sign-in' : 'Saturday crew', [UPPER]);
			const note = 'Which sign-in sheet: the paper one or the form?';
			await openChat(page, chat);
			const online = await offline(page, chat);
			await pressOffline(page, chat.ids[0], UPPER_QUOTE, note);
			await online();
			const posts = await held(page, chat);
			await openChat(page, chat);
			await expect(submit(page)).toHaveText('Sending…', { timeout: 15_000 });
			await expect(commentEditor(page)).toHaveValue(note);
			await expect(page.locator(`[data-message-id="${chat.ids[0]}"] .cm-comment-pending`)).toHaveText(UPPER_QUOTE);
			await shot(page, 'chat-sending');
			posts.release();
			await expect(commentBox(page)).toHaveCount(0, { timeout: 15_000 });
			await expect(placed(page, chat.ids[0], UPPER_QUOTE)).toHaveCount(1, { timeout: 15_000 });
			await shot(page, 'chat-posted');
			expect(new Set(posts.through).size).toBe(1);
		});
	}

	test('with two pressed, the lower opens and each posts exactly once (QC1) 393', async ({ page, request }) => {
		await page.setViewportSize({ width: 393, height: 852 });
		const chat = await seedChat(request, 'Trail day crew', [UPPER, LOWER]);
		await openChat(page, chat);
		const online = await offline(page, chat);
		await pressOffline(page, chat.ids[0], UPPER_QUOTE, 'Which sign-in sheet: the paper one or the form?');
		await page.getByRole('dialog', { name: 'New comment' }).getByRole('button', { name: 'Close', exact: true }).click();
		await expect(commentBox(page)).toHaveCount(0);
		await pressOffline(page, chat.ids[1], LOWER_QUOTE, 'Who brings the water jugs?');
		await online();
		const posts = await held(page, chat);
		await openChat(page, chat);
		await expect(commentEditor(page)).toHaveValue('Who brings the water jugs?', { timeout: 15_000 });
		await expect(submit(page)).toHaveText('Sending…');
		await expect(commentBox(page)).toHaveCount(1);
		await shot(page, 'chat-qc1-two-pressed');
		posts.release();
		await expect(commentBox(page)).toHaveCount(0, { timeout: 15_000 });
		await expect(placed(page, chat.ids[0], UPPER_QUOTE)).toHaveCount(1, { timeout: 15_000 });
		await expect(placed(page, chat.ids[1], LOWER_QUOTE)).toHaveCount(1, { timeout: 15_000 });
		await shot(page, 'chat-qc1-posted');
		expect(posts.through).toHaveLength(2);
		expect(new Set(posts.through).size).toBe(2);
	});

	// QC3: its passage no longer reads as kept (the text before it was kept differently, as if the message had changed), and
	// its post is already out when the chat is drawn, since the connection coming up sent it: the server's answer settles it.
	for (const quoteGone of [false, true]) {
		test(`one whose passage no longer reads as kept, its post out, opens unplaced reading Sending… and ${quoteGone ? 'shows the server’s refusal once' : 'posts once with no refusal'} (QC3) 393`, async ({ page, request }) => {
			await page.setViewportSize({ width: 393, height: 852 });
			const chat = await seedChat(request, quoteGone ? 'Crew briefing' : 'Crew roster', [UPPER]);
			const note = 'Which sign-in sheet: the paper one or the form?';
			await openChat(page, chat);
			const online = await offline(page, chat);
			await pressOffline(page, chat.ids[0], UPPER_QUOTE, note);
			await online();
			await page.addInitScript(() => {
				if (location.protocol === 'about:') return;
				for (const name of Object.keys(localStorage)) {
					if (!name.startsWith('corsia:commentDraft:v1:') || !name.includes(':chat:')) continue;
					const draft = JSON.parse(localStorage.getItem(name)!);
					if (draft.place && !draft.place.prefix.startsWith('Earlier: ')) draft.place.prefix = `Earlier: ${draft.place.prefix}`;
					localStorage.setItem(name, JSON.stringify(draft));
				}
				// Every refusal line a comment box shows, counted as it appears, so one that comes and goes is not missed.
				const seen = new Set<Element>();
				new MutationObserver(() => {
					for (const line of document.querySelectorAll('[role="dialog"] [role="alert"]')) seen.add(line);
					(window as unknown as { refusalLines: number }).refusalLines = seen.size;
				}).observe(document, { childList: true, subtree: true, characterData: true });
			});
			const refusalLines = () => page.evaluate(() => (window as unknown as { refusalLines?: number }).refusalLines ?? 0);
			const posts = await held(page, chat, quoteGone);
			await openChat(page, chat);
			const dialog = page.getByRole('dialog', { name: 'New comment' });
			await expect(commentEditor(page)).toHaveValue(note, { timeout: 15_000 });
			await expect(dialog.locator('button.submit')).toHaveText('Sending…');
			await expect(page.locator('.cm-comment-pending')).toHaveCount(0);
			expect(await refusalLines()).toBe(0);
			await shot(page, quoteGone ? 'chat-qc3-out-sending' : 'chat-qc3-out-accepted-sending');
			posts.release();
			if (quoteGone) {
				await expect(dialog.getByRole('alert')).toContainText('The text this comment quotes isn’t in the chat', { timeout: 15_000 });
				await page.waitForTimeout(1500);
				expect(await refusalLines()).toBe(1);
				await expect(page.locator('.toast')).toHaveCount(0);
				await expect(commentEditor(page)).toHaveValue(note);
				expect(posts.refused).toHaveLength(1);
				await expect(placed(page, chat.ids[0], UPPER_QUOTE)).toHaveCount(0);
				await shot(page, 'chat-qc3-out-refused');
			} else {
				await expect(commentBox(page)).toHaveCount(0, { timeout: 15_000 });
				await expect(placed(page, chat.ids[0], UPPER_QUOTE)).toHaveCount(1, { timeout: 15_000 });
				expect(await refusalLines()).toBe(0);
				expect(posts.through).toHaveLength(1);
			}
		});
	}
});
