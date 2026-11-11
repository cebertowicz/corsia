import path from 'node:path';
import { test } from '../support/test';
import { expect, type Locator, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';
import { isolatedDb } from '../support/heldComments';

/**
 * A phone reader taps a highlighted word and its comment opens in the sheet; a comment's New sign goes once the reader has
 * seen that comment, and the header's unread count agrees with the signs. Chromium with touch at 393 wide stands in for the
 * phone (WebKit is not run here), so each tap is a real touch: touchstart and touchend, then the caret and the click that follow.
 */

const TAG = '@flow:doc-phone-tap-comment @surface:documents';
const DOC = [
	'# Handover notes', '',
	'The conductors review every release before it ships, and the reviewer signs off on timing.', '',
	'Escalate blockers to the owner the same day. Nothing waits for a meeting.', '',
	'The last paragraph holds no comment at all and only gives the page some length to scroll.', '',
].join('\n');
/** Each comment's one-word quote, and what the reader asked on it. */
const WORDS = {
	conductor: 'Who is the conductor here?',
	Escalate: 'Escalate to whom?',
	timing: 'What timing exactly?',
	reviewer: 'Which reviewer?',
} as const;
type Word = keyof typeof WORDS;
const nth = (word: Word) => DOC.indexOf(word);

test.use({ viewport: { width: 393, height: 852 }, hasTouch: true, isMobile: true });
test.beforeEach(({}, testInfo) => {
	test.skip(!['chromium-desktop', 'webkit-mobile'].includes(testInfo.project.name), 'Size and touch are set by the spec itself');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
});

interface Opened { request: Page['request']; artifactId: string; threadId: string; ids: Record<Word, string> }

/** Seed the document with one one-word comment per word, then open it in the artifact pane on a phone. */
async function openDocument(page: Page, { unreadReply = [], unreadRoot = [] }: { unreadReply?: Word[]; unreadRoot?: Word[] } = {}): Promise<Opened> {
	const request = page.request, params = { token: TEST_TOKEN };
	const title = `phone-tap-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const threadId = await createSession(request, title);
	const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`, docPath = path.join(workspace!, fileName);
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId: string = (await seeded.json()).artifact.id;
	const ids = {} as Record<Word, string>;
	for (const word of Object.keys(WORDS) as Word[]) {
		const created = await request.post(`/api/artifacts/${artifactId}/comments`, {
			params, data: { body: WORDS[word], anchor_text: word, from_off: nth(word), to_off: nth(word) + word.length, delivery_policy: 'immediate' },
		});
		expect(created.ok(), await created.text()).toBeTruthy();
		ids[word] = (await created.json()).comment.id;
	}
	// An agent's reply to the reader's comment is an unread reply; an agent's own comment, made after the reader's, is an unread comment.
	for (const word of unreadReply) isolatedDb([
		"connection.execute(\"INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, author_thread_id, body, created_at, updated_at) VALUES (lower(hex(randomblob(16))), ?, ?, 'Maya Chen', 'agent', ?, 'Confirmed.', strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 seconds'), strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 seconds'))\", sys.argv[2:5])",
	], artifactId, ids[word], threadId);
	for (const word of unreadRoot) isolatedDb([
		"connection.execute(\"UPDATE doc_comments SET author='Maya Chen', author_kind='agent', author_thread_id=?, created_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 seconds'), updated_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 seconds') WHERE id=?\", sys.argv[2:4])",
	], threadId, ids[word]);
	await page.addInitScript(({ threadId, docPath, fileName, artifactId }) => {
		localStorage.setItem('corsia_theme', 'auto');
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'phone-tap-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'phone-tap-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName, artifactId });
	await loginByUrl(page, { waitForComposer: false });
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	for (const word of Object.keys(WORDS) as Word[]) await expect(highlight(page, word)).toBeVisible({ timeout: 15_000 });
	return { request, artifactId, threadId, ids };
}

/** The frame in both themes (the theme follows the emulated colour scheme), then back to light. */
async function frames(page: Page, testInfo: TestInfo, name: string) {
	for (const scheme of ['light', 'dark'] as const) {
		await page.emulateMedia({ colorScheme: scheme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', scheme);
		await saveEvidenceScreenshot(page, testInfo, `${name}-${scheme}`);
	}
	await page.emulateMedia({ colorScheme: 'light' });
}

const highlight = (page: Page, word: Word): Locator => page.locator('.cm-comment-anchor', { hasText: new RegExp(`^${word}$`) }).first();
const sheet = (page: Page) => page.locator('.drawer-panel');
const sign = (page: Page) => page.locator('[data-highlight-dot] [aria-label^="New"]');
const pill = (page: Page) => page.locator('.comment-status');

/** The sheet shows this word's comment, and no other thread's. */
async function expectOpenOn(page: Page, word: Word, why: string) {
	await expect(sheet(page), `${why}: the comment sheet opens`).toBeVisible();
	await expect(sheet(page).getByText(WORDS[word]), `${why}: it shows this word's comment`).toBeVisible();
	for (const other of (Object.keys(WORDS) as Word[]).filter((w) => w !== word)) await expect(sheet(page).getByText(WORDS[other]), `${why}: not ${other}'s`).toHaveCount(0);
}

async function putSheetAway(page: Page) {
	// One card's sheet closes with Close; the list's with Close comments.
	await sheet(page).getByRole('button', { name: /^Close( comments)?$/ }).tap();
	await expect(sheet(page)).toHaveCount(0);
}

/**
 * Where a phone puts the caret on a tap. iOS does not place it at the touch point as a desktop browser does: it takes the nearest end of
 * the tapped word, so a highlight that is only part of a word (a stem before its ending) is reached with the caret on either side of it,
 * never inside. Chromium places it at the point, so this script sets the word-end caret itself, once the touch's own mouse press has placed Chromium's.
 */
async function placeCaretAtWordEnds(page: Page) {
	await page.addInitScript(() => {
		let touched = 0;
		document.addEventListener('touchstart', () => { touched = Date.now(); }, true);
		document.addEventListener('mouseup', (event) => {
			if (Date.now() - touched > 1500) return;
			const at = document.caretRangeFromPoint(event.clientX, event.clientY);
			const line = at?.startContainer.parentElement?.closest('.cm-line');
			if (!at || !line) return;
			const before = document.createRange();
			before.setStart(line, 0);
			before.setEnd(at.startContainer, at.startOffset);
			const text = line.textContent ?? '', index = before.toString().length;
			let start = index, end = index;
			while (start > 0 && /\w/.test(text[start - 1])) start -= 1;
			while (end < text.length && /\w/.test(text[end])) end += 1;
			const target = index - start <= end - index ? start : end;
			const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
			let seen = 0;
			for (let node = walker.nextNode(); node; node = walker.nextNode()) {
				const length = node.textContent!.length;
				if (target <= seen + length) { document.getSelection()!.collapse(node, target - seen); return; }
				seen += length;
			}
		}, true);
	});
}

/** A press held on the highlight's right half for `ms`, as a thumb does; Playwright has no long-press of its own. */
async function longPress(page: Page, target: Locator, ms = 800) {
	const box = (await target.boundingBox())!;
	const point = [{ x: box.x + box.width * 0.75, y: box.y + box.height / 2, id: 1 }];
	const session = await page.context().newCDPSession(page);
	await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: point });
	await page.waitForTimeout(ms);
	await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
	await session.detach();
}

/** The right half of a highlight, where a word-end caret lands past a highlight that stops short of its word. */
const rightHalf = async (target: Locator) => ({ x: (await target.boundingBox())!.width * 0.75, y: 6 });

test.describe(`${TAG} tap`, () => {
	test('a tap on a one-word highlight opens its comment where the browser puts the caret at the point', async ({ page }, testInfo) => {
		test.setTimeout(60_000);
		await openDocument(page);
		await highlight(page, 'timing').tap();
		await expectOpenOn(page, 'timing', 'a tap on the word');
		await frames(page, testInfo, 'tap-word-opened');
	});

	test.describe('with the caret at the ends of the tapped word, as a phone places it', () => {
		test.beforeEach(async ({ page }) => { await placeCaretAtWordEnds(page); });

		test('a tap on a highlight that stops short of its word opens its comment', async ({ page }, testInfo) => {
			test.setTimeout(60_000);
			await openDocument(page);
			await highlight(page, 'conductor').tap({ position: await rightHalf(highlight(page, 'conductor')) });
			await expectOpenOn(page, 'conductor', 'a tap on the word');
			await frames(page, testInfo, 'tap-word-opened-ios-caret');
		});

		test('a tap at either edge of a one-word highlight opens its comment', async ({ page }) => {
			test.setTimeout(60_000);
			await openDocument(page);
			// The first word of a line: its left edge is where the line starts.
			await highlight(page, 'Escalate').tap({ position: { x: 1, y: 6 } });
			await expectOpenOn(page, 'Escalate', 'a tap at the left edge of a word that starts its line');
			await putSheetAway(page);
			// The last pixel of a highlight that is followed by the rest of its word.
			const box = (await highlight(page, 'conductor').boundingBox())!;
			await highlight(page, 'conductor').tap({ position: { x: box.width - 1, y: 6 } });
			await expectOpenOn(page, 'conductor', 'a tap at the right edge of a word followed by its ending');
		});

		test('a tap after the caret already sits in that word opens its comment again', async ({ page }) => {
			test.setTimeout(60_000);
			await openDocument(page);
			const word = highlight(page, 'conductor');
			await word.tap({ position: { x: 4, y: 6 } });
			await expectOpenOn(page, 'conductor', 'a tap on the left half, which puts the caret at the start of the word');
			await putSheetAway(page);
			await word.tap({ position: await rightHalf(word) });
			await expectOpenOn(page, 'conductor', 'a tap on the right half, with the caret already in the word');
		});

		test('a long press on a one-word highlight opens its comment', async ({ page, browserName }, testInfo) => {
			test.skip(browserName !== 'chromium', 'The long press is sent through the Chrome DevTools protocol');
			test.setTimeout(60_000);
			await openDocument(page);
			await longPress(page, highlight(page, 'conductor'));
			await expectOpenOn(page, 'conductor', 'a long press on the word');
			await frames(page, testInfo, 'long-press-opened');
		});
	});
});

test.describe(`${TAG} new sign`, () => {
	test('the sign clears once the card has been opened from its highlight, and stays for the unseen ones', async ({ page }, testInfo) => {
		test.setTimeout(60_000);
		await openDocument(page, { unreadReply: ['conductor'], unreadRoot: ['reviewer'] });
		await expect(sign(page)).toHaveCount(2);
		await expect(pill(page)).toHaveAccessibleName(/2 unread/);
		await frames(page, testInfo, 'signs-before');

		await highlight(page, 'conductor').tap();
		await expectOpenOn(page, 'conductor', 'opening the card from its highlight');
		await expect(sign(page), 'the opened comment has been seen; the other has not').toHaveCount(1);
		await expect(page.locator('[data-comment-new]')).toHaveCount(1);
		await putSheetAway(page);
		await expect(sign(page)).toHaveCount(1);
		await expect(pill(page)).toHaveAccessibleName(/1 unread/);
		await frames(page, testInfo, 'signs-after-one-seen');
	});

	test('opening the list shows every thread but marks none seen; a card tapped open in it is seen', async ({ page }, testInfo) => {
		test.setTimeout(60_000);
		await openDocument(page, { unreadReply: ['conductor'], unreadRoot: ['reviewer'] });
		await expect(sign(page)).toHaveCount(2);
		await pill(page).tap();
		await expect(sheet(page).getByText(WORDS.reviewer)).toBeVisible();
		await expect(sheet(page).getByText(WORDS.conductor)).toBeVisible();
		await frames(page, testInfo, 'list-open');
		// Listing is not opening: both are still new while the list is up, past the time a card would take to be read.
		await page.waitForTimeout(2500);
		await expect(pill(page)).toHaveAccessibleName(/2 unread/);

		await sheet(page).getByText(WORDS.conductor).tap();
		await expect(pill(page), 'the card the reader opened is seen').toHaveAccessibleName(/1 unread/);
		await putSheetAway(page);
		await expect(sign(page), 'the other thread is still new').toHaveCount(1);
		await expect(page.locator('[data-comment-new]')).toHaveCount(1);
		await frames(page, testInfo, 'signs-after-list-open');
	});
});
