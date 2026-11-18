import path from 'node:path';
import { type Page } from '@playwright/test';
import { expect, test } from '../support/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { centre, fingerDrag, painted, realTouchDrag } from '../support/sheetFinger';

/**
 * The phone comment list drops to a half sheet when the reader taps a card or its Go to text, so they can see the text it points
 * at; it then comes back up from anywhere along its head row (the title and the space around it), not only its thin grabber,
 * follows the finger while it rises, and stays up until it is pulled down or closed. A press on the head's own buttons stays
 * a button press. Chromium with touch at 393 wide, and WebKit at 393 when it is run (webkit-mobile), stand in for the phone.
 */

const TAG = '@flow:doc-comment-list-sheet-pull-up @surface:documents';
const PHONE = { width: 393, height: 852 };

/** Six commented passages and two suggestions, so the list is taller than any sheet and shows Accept all above its cards. */
const PASSAGES = Array.from({ length: 6 }, (_, i) => `Marker ${i + 1} stands where the trail meets the creek and needs a fresh coat of paint before the walk.`);
const SUGGESTED = ['Step one: the rollout', 'Step two: the rollout'];
const FILLER = (n: number) => `Section ${n}: the crew walked this stretch, noted the surface, and moved on to the next marker without stopping.`;
const DOC = ['# Trail markers', '', ...PASSAGES.flatMap((line, i) => [FILLER(i + 1), '', line, '']), ...SUGGESTED.flatMap((line) => [line, '']), FILLER(7), ''].join('\n');
const note = (n: number) => `Note ${n}: the paint on this marker has worn through on the side facing the water, so it needs a second coat.`;

test.use({ viewport: PHONE, hasTouch: true, isMobile: true });
test.beforeEach(({}, testInfo) => {
	test.skip(!['chromium-mobile', 'webkit-mobile'].includes(testInfo.project.name), 'The phone sheet is taken at 393 in the touch projects');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	test.setTimeout(180_000);
});

/** Seeds the document with a two-reply thread on each passage and two suggestions from another conversation, then opens it on a phone. */
async function openDocument(page: Page): Promise<string[]> {
	const request = page.request, params = { token: TEST_TOKEN };
	const title = `list-sheet-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const threadId = await createSession(request, title);
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
	const ids: string[] = [];
	for (const [i, quote] of PASSAGES.entries()) {
		const from = DOC.indexOf(quote);
		ids.push(await post({ body: note(i + 1), anchor_text: quote, from_off: from, to_off: from + quote.length }));
		for (const n of [1, 2]) await post({ body: `Reply ${n}: agreed, and the post itself leans a little.`, parent_id: ids[i] });
	}
	const suggester = await createSession(request, `${title}-suggester`);
	for (const quote of SUGGESTED) {
		const made = await request.post('/api/delegation/docs/comments', { params, data: { artifact_id: artifactId, quote, replacement: quote.replace('Step', 'Phase'), body: 'Use phase.', as_session_id: suggester } });
		expect(made.ok(), await made.text()).toBeTruthy();
	}
	await page.addInitScript(({ threadId, docPath, fileName, artifactId }) => {
		localStorage.setItem('corsia_theme', 'auto');
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'list-sheet-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'list-sheet-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName, artifactId });
	await loginByUrl(page, { waitForComposer: false });
	await page.getByRole('button', { name: /^Toggle sidebar/ }).tap();
	await page.getByText(title, { exact: true }).first().tap();
	await expect(page.locator('.cm-content', { hasText: 'Trail markers' })).toBeVisible({ timeout: 15_000 });
	return ids;
}

const sheet = (page: Page) => page.locator('[data-adaptive-drawer] .drawer-panel');
const head = (page: Page) => sheet(page).locator('.drawer-heading');
const card = (page: Page, id: string) => sheet(page).locator(`[data-comment-id="${id}"]`);
/** A let-go sheet settles in 300 ms (sheetDrag) and its height eases over 220 ms. */
const sheetSettled = async (page: Page) => { await page.waitForTimeout(450); await painted(page); };

/** The list sheet as it stands: its height, that height as a share of the column it is held in, and whether it rests at half. */
const sheetNow = (page: Page) => sheet(page).evaluate((panel) => {
	const column = Number.parseFloat((panel.closest('[data-adaptive-drawer]') as HTMLElement).style.getPropertyValue('--drawer-h')) || innerHeight;
	const height = panel.getBoundingClientRect().height;
	return { height, share: height / column, half: panel.classList.contains('half') };
});

/** Opens the list from the comments control; it opens full. */
async function openList(page: Page) {
	await page.locator('.comment-status').tap();
	await expect(sheet(page)).toHaveCount(1);
	await expect(head(page)).toBeVisible();
	await sheetSettled(page);
	expect((await sheetNow(page)).half, 'the list opens full').toBe(false);
}

/** At half, the sheet rests at about 46% of its column. */
async function expectHalf(page: Page, why: string) {
	await expect(sheet(page), why).toHaveClass(/\bhalf\b/);
	await sheetSettled(page);
	const rest = await sheetNow(page);
	expect(Math.abs(rest.share - 0.46), `${why}: it rests at about 46% of the column (${rest.share.toFixed(3)})`).toBeLessThanOrEqual(0.02);
	return rest;
}

/** Pulls the head row's title up 60 px in 10 px steps: the sheet grows with the finger during the move and is full once let go. */
async function pullUpFromHead(page: Page, rest: { height: number }) {
	const steps = [-10, -20, -30, -40, -50, -60];
	const seen = await fingerDrag(page, await centre(head(page).locator('h2')), steps, async () => (await sheetNow(page)).height);
	seen.forEach((height, i) => expect(Math.abs(height - (rest.height - steps[i])), `the sheet follows the finger at ${steps[i]} px (${height} from ${rest.height})`).toBeLessThanOrEqual(2));
	await sheetSettled(page);
	const raised = await sheetNow(page);
	expect(raised.half, 'let go, the sheet is full').toBe(false);
	expect(raised.height, 'and taller than at half').toBeGreaterThan(rest.height + 60);
	return raised;
}

/**
 * Where the document stands and what holds focus, once the document has stopped scrolling (a card tap scrolls to its text).
 * The first read marks the focused element, so `same` says whether focus is still on it.
 */
async function readerPlace(page: Page, mark = false): Promise<{ scrollTop: number; same: boolean }> {
	const read = () => page.evaluate((mark) => {
		let scroller = document.querySelector('.cm-content')?.parentElement ?? null;
		while (scroller && !(/(auto|scroll)/.test(getComputedStyle(scroller).overflowY) && scroller.scrollHeight > scroller.clientHeight)) scroller = scroller.parentElement;
		const held = window as unknown as { __readerFocus?: Element | null };
		if (mark) held.__readerFocus = document.activeElement;
		return { scrollTop: scroller?.scrollTop ?? -1, same: document.activeElement === held.__readerFocus };
	}, mark);
	let last = await read();
	for (let i = 0; i < 10; i++) {
		await page.waitForTimeout(150);
		const now = await read();
		if (now.scrollTop === last.scrollTop) return mark ? read() : now;
		last = now;
	}
	return last;
}

test.describe(`${TAG} list sheet`, () => {
	test('takes touch: the grabber and the head row are touch-action none, so a finger on them drags the sheet, not the page', async ({ page }) => {
		await openDocument(page);
		await openList(page);
		const actions = await sheet(page).evaluate((panel) => [panel.querySelector('.drawer-drag-region'), panel.querySelector('.drawer-heading')]
			.map((el) => el ? getComputedStyle(el).touchAction : 'missing'));
		expect(actions, 'grabber, then head row').toEqual(['none', 'none']);
	});

	test('takes touch: a real finger on the grabber, then on the head row, pulls the half sheet up while the document and focus stay put', async ({ page }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-mobile', 'Real touch input (CDP) is Chromium only');
		const ids = await openDocument(page);
		await openList(page);
		for (const [id, from, handle] of [[ids[2], 'the grabber', sheet(page).locator('.drawer-drag-region')], [ids[3], 'the head row', head(page).locator('h2')]] as const) {
			await card(page, id).locator('[data-discussion-original]').tap();
			const rest = await expectHalf(page, `a card tap before a pull on ${from}`);
			const before = await readerPlace(page, true);
			await realTouchDrag(page, await centre(handle), [-10, -20, -30, -40, -50, -60]);
			await sheetSettled(page);
			const raised = await sheetNow(page);
			expect(raised.half, `pulled up from ${from}, the sheet is full`).toBe(false);
			expect(raised.height, `and taller than at half (${raised.height} from ${rest.height})`).toBeGreaterThan(rest.height + 60);
			const after = await readerPlace(page);
			expect(after.scrollTop, `a pull on ${from} leaves the document where it was`).toBe(before.scrollTop);
			expect(after.same, `and focus where it was`).toBe(true);
		}
	});

	test('a card tap rests the list at half; its head row pulls it up with the finger, and it stays full through a list scroll', async ({ page }) => {
		const ids = await openDocument(page);
		await openList(page);
		await card(page, ids[2]).locator('[data-discussion-original]').tap();
		const rest = await expectHalf(page, 'a card tap');
		const raised = await pullUpFromHead(page, rest);
		await sheet(page).locator('.drawer-comments').evaluate((list) => { list.scrollTop += 240; list.dispatchEvent(new Event('scroll')); });
		await sheetSettled(page);
		const scrolled = await sheetNow(page);
		expect(scrolled.half, 'a list scroll leaves it full').toBe(false);
		expect(Math.abs(scrolled.height - raised.height), 'at the same height').toBeLessThanOrEqual(2);
	});

	test('Go to text rests the list at half too; its head row pulls it up, and a quick tap on the grabber does as well', async ({ page }) => {
		const ids = await openDocument(page);
		await openList(page);
		await card(page, ids[3]).locator('[data-comment-jump]').tap();
		const rest = await expectHalf(page, 'Go to text');
		await pullUpFromHead(page, rest);

		await card(page, ids[1]).locator('[data-comment-jump]').tap();
		await expectHalf(page, 'Go to text again');
		await fingerDrag(page, await centre(sheet(page).locator('.drawer-drag-region')), [], async () => null);
		await sheetSettled(page);
		expect((await sheetNow(page)).half, 'a quick tap on the grabber brings it to full').toBe(false);
	});

	test('from the head row a short pull down settles back, a long one closes, and the head\'s Close stays a button', async ({ page }) => {
		const ids = await openDocument(page);
		await openList(page);
		await card(page, ids[2]).locator('[data-discussion-original]').tap();
		const rest = await expectHalf(page, 'a card tap');

		await fingerDrag(page, await centre(head(page).locator('h2')), [10, 20, 30], async () => null, 400);
		await sheetSettled(page);
		const settled = await sheetNow(page);
		expect(settled.half, 'a short pull down leaves it at half').toBe(true);
		expect(Math.abs(settled.height - rest.height), 'back at its rest').toBeLessThanOrEqual(2);

		await fingerDrag(page, await centre(head(page).locator('h2')), [40, 80, 120, 160, 200, 260, 320, 400], async () => null);
		await expect(sheet(page), 'a long pull down closes it').toHaveCount(0);

		// A tap on Close at half closes the list; it is never taken for a tap that grows the sheet.
		await openList(page);
		await card(page, ids[2]).locator('[data-discussion-original]').tap();
		await expectHalf(page, 'a card tap');
		await head(page).getByRole('button', { name: 'Close comments', exact: true }).tap();
		await expect(sheet(page), 'Close closes the list').toHaveCount(0);
	});
});
