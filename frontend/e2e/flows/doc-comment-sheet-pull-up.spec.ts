import fs from 'node:fs';
import path from 'node:path';
import { type Locator, type Page } from '@playwright/test';
import { expect, test } from '../support/test';
import { createSession, loginByUrl, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage, seedMockResponses } from '../support/mock';
import { commentEditor, commentOnMessage, openCommentThread } from '../support/chatComments';
import { selectPhrase } from '../support/chatCommentFrames';
import { waitForMainSlideToSettle } from '../support/sidebar';

/**
 * A phone comment sheet held short to leave its text room above it grows when the reader pulls its grabber up, and stays up
 * until it closes: nothing moves the text under the reader while they pull, nothing caps it again when the keyboard comes up,
 * and every message, the reply box and the controls of a tall thread can be reached. The phone navigation slides over an open
 * sheet without dismissing it. Chromium with touch at 393 wide stands in for the phone (WebKit is not run here); the keyboard is
 * stood in for by shrinking the viewport to the height a phone keyboard leaves.
 */

const TAG = '@flow:doc-comment-sheet-pull-up @surface:documents';
const PHONE = { width: 393, height: 852 };
/** The page a phone keyboard leaves at 393 wide. */
const KEYBOARD_UP = { width: 393, height: 516 };
/** Frames for the design review are written here when it is set; CI keeps its own artifacts. */
const FRAMES = process.env.CORSIA_SHEET_FRAMES;

/** A table row many lines tall at 393: the press keeps only its top line above the sheet. */
const TALL_CELL = 'The crossing at the second bridge needs a new sign, a wider step down to the gravel, a railing on the creek side, and a second look at the drainage after the spring rains have passed.';
/** A paragraph two or three lines tall at 393: the press keeps all of it above the sheet. */
const PASSAGE = 'Volunteers meet at the north gate at eight and bring their own gloves and water.';
const FILLER = Array.from({ length: 24 }, (_, i) => `Section ${i + 1}: the crew walked this stretch, noted the surface, and moved on to the next marker without stopping.`);
const DOC = [
	'# Trail survey', '',
	...FILLER.flatMap((line) => [line, '']),
	'| Area | Review note |', '| --- | --- |',
	`| Bridge | ${TALL_CELL} |`,
	'| Signs | Plain words in a cell. |', '',
	PASSAGE, '',
	...FILLER.slice(0, 6).flatMap((line) => [line, '']),
].join('\n');
/** A reply long enough that a thread of several is taller than any sheet a phone can show. */
const reply = (n: number) => `Reply ${n}: we walked it again this morning and the notes above still hold, though the far end of the crossing needs another look before the volunteers arrive, and the sign should face the parking lot.`;

test.use({ viewport: PHONE, hasTouch: true, isMobile: true });
test.beforeEach(({}, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-mobile', 'The phone sheet is taken at 393 in the touch project');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	test.setTimeout(180_000);
});

type Opened = { title: string; other: string; ids: { cell: string; passage: string } };

/** Seeds the document with a tall thread on the table cell and on the passage, and a second conversation, then opens the document on a phone. */
async function openDocument(page: Page): Promise<Opened> {
	const request = page.request, params = { token: TEST_TOKEN };
	const title = `sheet-pull-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const threadId = await createSession(request, title);
	const other = `${title}-other`;
	await createSession(request, other);
	const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`, docPath = path.join(workspace!, fileName);
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId: string = (await seeded.json()).artifact.id;
	const post = async (data: Record<string, unknown>) => {
		const created = await request.post(`/api/artifacts/${artifactId}/comments`, { params, data: { delivery_policy: 'immediate', ...data } });
		expect(created.ok(), await created.text()).toBeTruthy();
		return (await created.json()).comment.id as string;
	};
	const ids = {} as Opened['ids'];
	for (const [key, quote] of [['cell', TALL_CELL], ['passage', PASSAGE]] as const) {
		const from = DOC.indexOf(quote);
		ids[key] = await post({ body: `Can we settle this before the walk? ${quote}`, anchor_text: quote, from_off: from, to_off: from + quote.length });
		for (let n = 1; n <= 5; n += 1) await post({ body: reply(n), parent_id: ids[key] });
	}
	await page.addInitScript(({ threadId, docPath, fileName, artifactId }) => {
		localStorage.setItem('corsia_theme', 'auto');
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'sheet-pull-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'sheet-pull-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName, artifactId });
	await loginByUrl(page, { waitForComposer: false });
	await page.getByRole('button', { name: /^Toggle sidebar/ }).tap();
	await page.getByText(title, { exact: true }).first().tap();
	await expect(cellMark(page)).toBeVisible({ timeout: 15_000 });
	await expect(passageMark(page)).toBeVisible();
	return { title, other, ids };
}

const cellMark = (page: Page) => page.locator('.cm-md-table .cm-comment-anchor').first();
const passageMark = (page: Page) => page.locator('.cm-comment-anchor', { hasText: 'Volunteers meet' }).first();
const sheet = (page: Page) => page.locator('[data-adaptive-drawer] .drawer-panel');
const sidebar = (page: Page) => page.locator('aside.sidebar');

/** Two frames, so whatever the last step changed has been laid out and painted. */
const painted = (page: Page) => page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
/** A let-go sheet settles in 300 ms (sheetDrag) and its height eases over 220 ms. */
const sheetSettled = async (page: Page) => { await page.waitForTimeout(450); await painted(page); };

/** Frames for the design review, in both themes, when CORSIA_SHEET_FRAMES is set. */
async function frame(page: Page, name: string) {
	if (!FRAMES) return;
	fs.mkdirSync(FRAMES, { recursive: true });
	for (const scheme of ['light', 'dark'] as const) {
		await page.emulateMedia({ colorScheme: scheme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', scheme);
		await painted(page);
		await page.screenshot({ path: path.join(FRAMES, `${name}-${scheme}.png`), animations: 'disabled' });
	}
	await page.emulateMedia({ colorScheme: 'light' });
}

/** Where the text under the reader is: the scroller holding it, and the first line of the highlight. */
const place = (mark: Locator) => mark.evaluate((el) => {
	let scroller = el.parentElement;
	while (scroller && !(/(auto|scroll)/.test(getComputedStyle(scroller).overflowY) && scroller.scrollHeight > scroller.clientHeight)) scroller = scroller.parentElement;
	const line = el.getClientRects()[0] ?? el.getBoundingClientRect();
	return { scrollTop: scroller?.scrollTop ?? -1, top: line.top, left: line.left, height: el.getBoundingClientRect().height };
});

/** The open sheet as it stands: its height, the cap it is held to (none once the reader pulled it up) and how much of its body is out of sight. */
const sheetNow = (page: Page) => sheet(page).last().evaluate((panel) => {
	const body = panel.querySelector<HTMLElement>(':scope > .drawer-body');
	return {
		height: panel.getBoundingClientRect().height,
		cap: panel.style.getPropertyValue('--drawer-text-cap'),
		hidden: body ? body.scrollHeight - body.clientHeight : 0,
	};
});

/** Scrolls a tall row's bottom to the foot of the visible document, as a reader who has read down to it. */
async function bringLowest(page: Page, mark: Locator) {
	await mark.evaluate((el) => {
		let scroller = el.parentElement;
		while (scroller && !(/(auto|scroll)/.test(getComputedStyle(scroller).overflowY) && scroller.scrollHeight > scroller.clientHeight)) scroller = scroller.parentElement;
		if (!scroller) throw new Error('the highlight has no scroller');
		const row = (el.closest('tr') ?? el).getBoundingClientRect();
		scroller.scrollTop += row.bottom - (Math.min(scroller.getBoundingClientRect().bottom, visualViewport?.height ?? innerHeight) - 12);
	});
	await painted(page);
}

/** Taps a highlight on its first line, as a phone reader does, and waits for the thread sheet to come to rest. */
async function tapOpen(page: Page, mark: Locator) {
	const line = await mark.evaluate((el) => { const r = el.getClientRects()[0]; return { x: r.left + Math.min(24, r.width / 2), y: r.top + r.height / 2 }; });
	await page.touchscreen.tap(line.x, line.y);
	await expect(sheet(page)).toHaveCount(1);
	await expect(sheet(page).locator('[data-discussion-original]')).toBeVisible();
	await sheetSettled(page);
}

/**
 * Drags the open sheet's grabber with the primary button, `steps` being the travel from the press after each move (negative up),
 * reading `each` after every step; `hold` waits before letting go, so a short pull is not a quick tap.
 */
async function pull<T>(page: Page, steps: number[], each: () => Promise<T>, hold = 0): Promise<T[]> {
	const grabber = await sheet(page).last().locator('.drawer-drag-region').boundingBox();
	expect(grabber, 'the sheet has a grabber').not.toBeNull();
	const x = grabber!.x + grabber!.width / 2, y = grabber!.y + grabber!.height / 2;
	const seen: T[] = [];
	await page.mouse.move(x, y);
	await page.mouse.down();
	for (const dy of steps) {
		await page.mouse.move(x, y + dy);
		await painted(page);
		seen.push(await each());
	}
	if (hold) await page.waitForTimeout(hold);
	await page.mouse.up();
	await sheetSettled(page);
	return seen;
}

/**
 * Every message, the reply box and every control in the sheet, each scrolled into its own part of the sheet the way a reader
 * scrolls it, and the element hit at its centre. A miss names what was hit instead.
 */
const unreachable = (page: Page) => sheet(page).last().evaluate((panel) => {
	const label = (el: Element | null) => el ? `${el.tagName.toLowerCase()}${el.className && typeof el.className === 'string' ? `.${el.className.trim().split(/\s+/).join('.')}` : ''}${el.getAttribute('aria-label') ? `[${el.getAttribute('aria-label')}]` : ''}` : 'nothing';
	const shown = (el: Element) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
	const targets = [...panel.querySelectorAll<HTMLElement>('[data-discussion-original], .ct-reply, textarea, input, button:not(:disabled)')].filter(shown);
	const bottom = visualViewport?.height ?? innerHeight;
	const misses: string[] = [];
	for (const el of targets) {
		let scroller = el.parentElement;
		while (scroller && scroller !== panel && !(/(auto|scroll)/.test(getComputedStyle(scroller).overflowY) && scroller.scrollHeight > scroller.clientHeight)) scroller = scroller.parentElement;
		if (scroller && scroller !== panel) {
			const s = scroller.getBoundingClientRect(), r = el.getBoundingClientRect();
			scroller.scrollTop += r.top + r.height / 2 - (s.top + s.height / 2);
		}
		const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
		const hit = y >= 0 && y <= bottom ? document.elementFromPoint(x, y) : null;
		if (!hit || !(hit === el || el.contains(hit))) misses.push(`${label(el)} "${(el.textContent ?? '').trim().slice(0, 24)}" at ${Math.round(x)},${Math.round(y)} hits ${label(hit)}`);
	}
	return { checked: targets.length, misses };
});

/** At 393 every field a reader types in reads at 16 px or more (no zoom on focus), and nothing is wider than the screen. */
async function expectPhoneFit(page: Page, why: string) {
	const found = await page.evaluate(() => {
		const label = (el: Element) => `${el.tagName.toLowerCase()}${typeof el.className === 'string' && el.className ? `.${el.className.trim().split(/\s+/).join('.')}` : ''}`;
		const shown = (el: Element) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
		const fields = [...document.querySelectorAll('textarea, select, [contenteditable="true"], [contenteditable=""], [contenteditable="plaintext-only"], input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=range]):not([type=file]):not([type=color])')]
			.filter(shown).map((el) => ({ el: label(el), size: Number.parseFloat(getComputedStyle(el).fontSize) })).filter((field) => field.size < 16);
		const width = innerWidth, wide: string[] = [];
		for (const el of document.querySelectorAll('body *')) {
			const r = el.getBoundingClientRect();
			if (r.width <= width + 1 || !shown(el)) continue;
			let clipped = false;
			for (let up = el.parentElement; up; up = up.parentElement) {
				if (getComputedStyle(up).overflowX !== 'visible' && up.getBoundingClientRect().width <= width + 1) { clipped = true; break; }
			}
			if (!clipped) wide.push(`${label(el)} ${Math.round(r.width)}`);
		}
		return { fields, wide, scrollWidth: document.documentElement.scrollWidth, width };
	});
	expect(found.fields, `${why}: every editable surface computes at 16 px or more`).toEqual([]);
	expect(found.wide, `${why}: nothing is wider than the viewport`).toEqual([]);
	expect(found.scrollWidth, `${why}: the page does not scroll sideways`).toBeLessThanOrEqual(found.width);
}

test.describe(`${TAG} document`, () => {
	test('a tall table-cell thread lowest on screen keeps every message, the reply box and the controls reachable with the keyboard up', async ({ page }) => {
		await openDocument(page);
		await expectPhoneFit(page, 'sheet closed');
		await bringLowest(page, cellMark(page));
		await tapOpen(page, cellMark(page));
		await sheet(page).last().locator('textarea').first().tap();
		await page.setViewportSize(KEYBOARD_UP);
		await sheetSettled(page);
		await frame(page, 'tall-cell-keyboard-up');
		const reach = await unreachable(page);
		expect(reach.checked, 'the thread shows its messages, its reply box and its controls').toBeGreaterThanOrEqual(8);
		expect(reach.misses, 'each one is on screen and takes the touch at its centre').toEqual([]);
		await expectPhoneFit(page, 'sheet open, keyboard up');
		await page.setViewportSize(PHONE);
		await sheetSettled(page);
		await expectPhoneFit(page, 'sheet open');
	});

	test('a capped sheet follows the finger up without moving the text, grows on release, and a pull down closes it', async ({ page }) => {
		await openDocument(page);
		await bringLowest(page, passageMark(page));
		await tapOpen(page, passageMark(page));
		const rest = await sheetNow(page), at = await place(passageMark(page));
		expect(rest.cap, 'the sheet is held below its height to leave the passage room above it').not.toBe('');
		expect(Math.abs(rest.height - Number.parseFloat(rest.cap)), `the sheet rests at its cap (${rest.height} vs ${rest.cap})`).toBeLessThanOrEqual(1);
		expect(rest.hidden, 'and its thread runs on past what it shows').toBeGreaterThan(1);
		await frame(page, 'capped-at-rest');

		// A short, slow pull is let go: nothing moves, and the sheet stays at its cap.
		const short = await pull(page, [-10, -20, -30], () => place(passageMark(page)), 400);
		for (const step of short) expect(step, 'the passage stays put during a short pull').toEqual(at);
		const afterShort = await sheetNow(page);
		expect(afterShort.cap, 'a short pull leaves the cap').toBe(rest.cap);
		expect(Math.abs(afterShort.height - rest.height), 'and the sheet back at its rest').toBeLessThanOrEqual(1);
		expect(await place(passageMark(page)), 'nothing under the reader moved').toEqual(at);

		// A slow pull in 10 px steps: the sheet follows the finger, the passage under the reader never moves.
		const steps = [-10, -20, -30, -40, -50, -60, -70, -80];
		const seen = await pull(page, steps, async () => ({ place: await place(passageMark(page)), height: (await sheetNow(page)).height }));
		const raised = await sheetNow(page);
		expect(raised.cap, 'let go past 40 px, the sheet drops its cap').toBe('');
		expect(raised.height, 'and grows').toBeGreaterThan(rest.height + 20);
		seen.forEach((step, i) => {
			expect(step.place, `the passage stays put at ${steps[i]} px`).toEqual(at);
			expect(Math.abs(step.height - Math.min(rest.height - steps[i], raised.height)), `the sheet follows the finger at ${steps[i]} px (${step.height})`).toBeLessThanOrEqual(2);
		});
		expect(await place(passageMark(page)), 'once grown, the document has not scrolled').toEqual(at);
		await frame(page, 'raised');

		// Pulled down, the sheet closes; the document stays where it was.
		await pull(page, [40, 80, 120, 160, 200, 260, 320, 400], async () => null);
		await expect(sheet(page)).toHaveCount(0);
		expect((await place(passageMark(page))).scrollTop, 'closing by a pull leaves the document where it was').toBe(at.scrollTop);
		await expectPhoneFit(page, 'sheet closed after a pull down');
	});

	test('a raised sheet stays raised when the reply box takes focus and the keyboard comes up', async ({ page }) => {
		await openDocument(page);
		await bringLowest(page, passageMark(page));
		await tapOpen(page, passageMark(page));
		expect((await sheetNow(page)).cap, 'the sheet opens held to its cap').not.toBe('');
		await pull(page, [-20, -40, -60, -80], async () => null);
		expect((await sheetNow(page)).cap, 'pulled up, it drops the cap').toBe('');
		const at = await place(passageMark(page));
		await sheet(page).last().locator('textarea').first().tap();
		await page.setViewportSize(KEYBOARD_UP);
		await sheetSettled(page);
		await expect(sheet(page).last().locator('textarea').first()).toBeFocused();
		expect((await sheetNow(page)).cap, 'the keyboard does not cap a raised sheet again').toBe('');
		expect((await place(passageMark(page))).scrollTop, 'and moves no text under the reader').toBe(at.scrollTop);
		await frame(page, 'raised-keyboard-up');
		const reach = await unreachable(page);
		expect(reach.misses, 'everything in the raised sheet takes the touch').toEqual([]);
		await page.setViewportSize(PHONE);
	});

	test('the phone navigation slides over an open sheet and gives it back as it was', async ({ page }) => {
		const { other, ids } = await openDocument(page);

		// A thread sheet is modal: its scrim covers the screen edge and the menu button, so the navigation cannot come over it.
		await bringLowest(page, cellMark(page));
		await tapOpen(page, cellMark(page));
		const modal = await page.evaluate(() => {
			const menu = document.querySelector('[aria-label^="Toggle sidebar"]')!.getBoundingClientRect();
			const at = (x: number, y: number) => !!document.elementFromPoint(x, y)?.closest('[data-adaptive-drawer]');
			return { edge: at(8, innerHeight / 2), menu: at(menu.left + menu.width / 2, menu.top + menu.height / 2) };
		});
		expect(modal, 'the thread sheet\'s scrim takes the edge swipe and the menu button').toEqual({ edge: true, menu: true });
		await sheet(page).last().getByRole('button', { name: /^Close/ }).first().tap();
		await expect(sheet(page)).toHaveCount(0);

		// The list, jumped from to a comment, rests as a half sheet the reader can reach past; they start a reply in it.
		await page.locator('.comment-status').tap();
		await expect(sheet(page)).toHaveCount(1);
		await sheet(page).locator(`[data-comment-id="${ids.passage}"] [data-comment-jump]`).tap();
		await expect(sheet(page)).toHaveClass(/\bhalf\b/);
		await sheetSettled(page);
		const field = sheet(page).locator(`[data-comment-id="${ids.passage}"] textarea`).first();
		await field.tap();
		await page.keyboard.type('Draft kept under the navigation');
		await sheetSettled(page);
		const listPlace = () => sheet(page).evaluate((panel) => {
			const card = panel.querySelector<HTMLElement>('[data-comment-id].focused');
			let scroller = card?.parentElement ?? null;
			while (scroller && scroller !== panel && !(/(auto|scroll)/.test(getComputedStyle(scroller).overflowY) && scroller.scrollHeight > scroller.clientHeight)) scroller = scroller.parentElement;
			const active = document.activeElement as HTMLElement | null;
			return { card: card?.dataset.commentId ?? null, scrollTop: scroller?.scrollTop ?? -1, focusInside: !!active && panel.contains(active), focusTag: active?.tagName ?? null };
		});
		const before = await listPlace();
		expect(before.card, 'the jumped-to card is the open one').toBe(ids.passage);
		expect(before.focusInside, 'the reply box holds focus in the sheet').toBe(true);

		const openNav = async () => {
			await page.getByRole('button', { name: /^Toggle sidebar/ }).tap();
			await expect(sidebar(page)).toHaveClass(/\bopen\b/);
			await waitForMainSlideToSettle(page);
		};
		const navClosed = async () => {
			await expect(sidebar(page)).not.toHaveClass(/\bopen\b/);
			await waitForMainSlideToSettle(page);
			await sheetSettled(page);
		};
		const expectGivenBack = async (why: string) => {
			await expect(page.locator('[data-adaptive-drawer]'), `${why}: the sheet is uncovered`).not.toHaveClass(/\bcovered\b/);
			expect(await listPlace(), `${why}: same card, same scroll, focus back inside`).toEqual(before);
			await expect(field, `${why}: the draft is kept`).toHaveValue('Draft kept under the navigation');
			await expect(field, `${why}: focus is back in the reply box`).toBeFocused();
		};

		// The navigation comes over the sheet: the sheet is covered, and the navigation's rows take the touch.
		await openNav();
		await expect(page.locator('[data-adaptive-drawer]')).toHaveClass(/\bcovered\b/);
		const rows = await sidebar(page).evaluate((aside) => {
			const shown = [...aside.querySelectorAll<HTMLElement>('a, button')].filter((el) => {
				const r = el.getBoundingClientRect();
				return r.width > 0 && r.height > 0 && r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight && getComputedStyle(el).visibility !== 'hidden';
			});
			return { count: shown.length, misses: shown.filter((el) => { const r = el.getBoundingClientRect(); const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return !hit || !(hit === el || el.contains(hit)); }).map((el) => el.getAttribute('aria-label') ?? el.textContent?.trim().slice(0, 30)) };
		});
		expect(rows.count, 'the navigation shows its rows').toBeGreaterThanOrEqual(3);
		expect(rows.misses, 'every navigation row takes the touch over the sheet').toEqual([]);
		await frame(page, 'nav-over-sheet');

		// Closed by a tap beside it, the navigation gives the sheet back as it was.
		await page.touchscreen.tap(PHONE.width - 12, PHONE.height / 2);
		await navClosed();
		await expectGivenBack('after the navigation closes');
		await frame(page, 'sheet-after-nav');

		// Back with the navigation over the sheet closes the navigation only.
		await openNav();
		await page.goBack();
		await navClosed();
		await expect(sheet(page)).toHaveCount(1);
		await expectGivenBack('after Back');

		// Escape likewise.
		await openNav();
		await page.keyboard.press('Escape');
		await navClosed();
		await expect(sheet(page)).toHaveCount(1);
		await expectGivenBack('after Escape');

		// Picking another conversation in the navigation leaves no sheet over the new view.
		await openNav();
		await sidebarThread(page, other).tap();
		await navClosed();
		await expect(page.locator('[data-adaptive-drawer]'), 'no sheet over the other conversation').toHaveCount(0);
		await frame(page, 'other-conversation');
	});
});

test.describe(`${TAG} chat`, () => {
	test.use({ seedPerTest: true });

	/** A passage two or three lines tall at 393, so the sheet leaves it room above itself. */
	const QUOTE = 'the north gate opens at eight, and each volunteer brings gloves, water, and a printed map of the loop';
	const NOTE = Array.from({ length: 6 }, (_, i) => `Point ${i + 1}: the map in the shed is out of date, so print the new one and check the loop markers before the walk.`).join(' ');

	test('the opened chat comment sheet pulls up with the keyboard up, and the transcript stays put', async ({ page, request }) => {
		await page.addInitScript(() => localStorage.setItem('corsia_theme', 'auto'));
		const title = `Sheet pull chat ${crypto.randomUUID()}`;
		const thread = await createSession(request, title);
		await injectMessage(request, thread, { role: 'assistant', content: 'Earlier context.\n\n'.repeat(24), metadata: { for_user: true } }, TEST_TOKEN);
		const id = await injectMessage(request, thread, { role: 'assistant', content: `For Saturday: ${QUOTE}.`, metadata: { for_user: true } }, TEST_TOKEN);
		await seedMockResponses(request, [{ content: '[c2.1] Noted.' }]);
		await openAppPastLanding(page);
		await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
		await page.getByRole('button', { name: /^Toggle sidebar/ }).tap();
		await sidebarThread(page, title).tap();
		await waitForWsConnected(page);
		const row = page.locator(`[data-message-id="${id}"]`);
		await expect(row).toBeVisible();
		await selectPhrase(page, id, QUOTE);
		await commentEditor(page).fill(NOTE);
		await page.getByRole('dialog', { name: 'New comment' }).getByRole('button', { name: 'Comment', exact: true }).tap();
		await commentOnMessage(row, QUOTE);

		await openCommentThread(row, page, QUOTE, { tap: true });
		await sheetSettled(page);
		const mark = row.locator('.cm-comment-anchor').filter({ hasText: QUOTE });
		await sheet(page).last().locator('textarea').first().tap();
		await page.setViewportSize(KEYBOARD_UP);
		await sheetSettled(page);
		const rest = await sheetNow(page), at = await place(mark);
		expect(rest.cap, 'with the keyboard up, the sheet is held below its height to leave the passage room').not.toBe('');
		expect(rest.hidden, 'and its thread runs on past what it shows').toBeGreaterThan(1);
		await frame(page, 'chat-capped-keyboard-up');

		const steps = [-10, -20, -30, -40, -50, -60, -70, -80];
		const seen = await pull(page, steps, () => place(mark));
		for (const [i, step] of seen.entries()) expect(step, `the passage stays put at ${steps[i]} px`).toEqual(at);
		const raised = await sheetNow(page);
		expect(raised.cap, 'let go past 40 px, the sheet drops its cap').toBe('');
		expect(raised.height, 'and grows').toBeGreaterThan(rest.height + 20);
		expect((await place(mark)).scrollTop, 'the transcript has not scrolled').toBe(at.scrollTop);
		await frame(page, 'chat-raised-keyboard-up');
		await page.setViewportSize(PHONE);
		await sheetSettled(page);
		expect((await sheetNow(page)).cap, 'the keyboard going down does not cap it again').toBe('');
	});
});
