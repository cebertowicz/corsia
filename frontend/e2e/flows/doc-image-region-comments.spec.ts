import fs from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';
import { syntheticPng } from '../support/syntheticImages';
import { drag, sendFromDrawerByKeyboard, settled, shoot } from '../support/imageRegionComments';
import { leaveUserCommentsUnsent } from '../support/docFeedbackFooter';
import { installPaintedFrames, type EachPaintedFrame } from '../support/paintedFrames';

// Comments on parts of an image inside a document (DCOL-FR13.3, 13.5, 13.6, 13.12): the viewer over the document,
// the margin marker and drawer group back in it, and the one feedback batch both show and send.

const NATURAL = { width: 1600, height: 1000 };
const REPLACED_NATURAL = { width: 1200, height: 800 };
const filler = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => `Paragraph ${from + i}: the walkthrough continues with enough words to wrap across a few lines of the document column.\n`).join('\n');
const DOC = ['# Design walkthrough', '', filler(1, 4), '![Mock screen](shots/screen.png)', '', filler(4, 12)].join('\n');
const LIVE_REFRESH_DOC = DOC.replace('screen.png', 'screen.svg');
const svgImage = ({ width, height }: typeof NATURAL) => `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" fill="teal" /></svg>`;
/** Two images, for many comments on each and a drawer with more than one image group. */
const DOC_TWO = ['# Design walkthrough', '', filler(1, 2), '![Mock screen](shots/screen.png)', '', filler(2, 4), '![Settings screen](shots/settings.png)', '', filler(4, 12)].join('\n');
const query = `?token=${encodeURIComponent(TEST_TOKEN)}`;

interface Seeded { sessionTitle: string; sessionId: string; artifactId: string; imagePath: string }

/** A document with one image near its top, open in its thread's canvas. */
async function seedDocument(page: Page, request: APIRequestContext, label: string, splitWidth: number | null, doc = DOC): Promise<Seeded> {
	const stamp = Date.now();
	const sessionTitle = `${label}-${stamp}`;
	const sessionId = await createSession(request, sessionTitle);
	const tree = await (await request.get(`/api/files/tree${query}`)).json();
	const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
	const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
	expect(workspaceRoot).toEqual(expect.any(String));
	const dir = path.join(workspaceRoot, `${label}-${stamp}`);
	const docPath = path.join(dir, 'walkthrough.md');
	fs.mkdirSync(path.join(dir, 'shots'), { recursive: true });
	for (const name of ['screen.png', 'settings.png']) fs.writeFileSync(path.join(dir, 'shots', name), syntheticPng(NATURAL.width, NATURAL.height));
	fs.writeFileSync(docPath, doc);
	const registered = await request.post(`/api/files/register${query}`, { data: { path: docPath, thread_id: sessionId } });
	expect(registered.ok(), `register failed: ${registered.status()}`).toBeTruthy();
	const artifactId = (await registered.json()).artifact.id as string;
	await page.addInitScript(
		({ threadId, id, tabPath, width }) => {
			localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
				threads: { [threadId]: {
					tabs: [{ instanceId: 'tab-doc', artifactId: id, path: tabPath, title: 'walkthrough.md', loadedRevision: null, latestRevision: null, status: 'loading' }],
					activeTabId: 'tab-doc', visible: true, activeSurface: 'artifact',
				} },
				splitWidth: width,
			}));
		},
		{ threadId: sessionId, id: artifactId, tabPath: docPath, width: splitWidth },
	);
	return { sessionTitle, sessionId, artifactId, imagePath: path.join(dir, 'shots', 'screen.png') };
}

const overlay = (page: Page) => page.locator('.image-overlay');
const bar = (page: Page) => overlay(page).locator('.bar');
const docImage = (page: Page) => page.locator('.cm-md-image img').first();
const marker = (page: Page) => page.locator('.cm-comment-marker').first();
const drawer = (page: Page) => page.locator('.canvas-comment-drawer, .drawer-panel[role="dialog"]');
const tag = (page: Page, number: number) => overlay(page).locator(`[data-region-tag="${number}"]:not([inert])`);
const viewerSend = (page: Page) => bar(page).getByRole('button', { name: /^Send/ });
const docComments = (page: Page) => page.locator('.document-header .comment-status');
/** The document's pending batch, as its header pill names it; its Send now is in the comments drawer's footer. */
const unsentName = (count: number) => new RegExp(`, ${count} messages? not sent yet$`);
const drawerThumb = (page: Page) => drawer(page).locator('[data-region-thumb]').first();
/** The document drawer's footer: the one place an in-document image's batch is sent from (RSR-10); the viewer has no Send. */
const footer = (page: Page) => drawer(page).locator('[data-drawer-unsent]');
const footerSend = (page: Page) => footer(page).getByRole('button', { name: 'Send now', exact: true });
/** The document's own delivery row: what was sent, or not delivered with Retry. */
const docDelivery = (page: Page) => page.locator('.document-header .feedback-controls');
/** Back from the viewer to the document, then its comments drawer opened from the header pill. */
async function backToDocumentDrawer(page: Page) {
	await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
	await expect(overlay(page)).toHaveCount(0);
	await docComments(page).click();
	await expect(footer(page)).toBeVisible();
}
/** The document's comments drawer closed with its own Close button. */
async function closeDocumentDrawer(page: Page) {
	await drawer(page).getByRole('button', { name: 'Close comments', exact: true }).click();
	await expect(drawer(page)).toHaveCount(0);
}
/** The document drawer's list, which keeps its own scroll. */
const drawerList = (page: Page) => page.locator('[data-doc-comments-drawer] .drawer-comments');
/**
 * J3 from the drawer: a card opens the viewer at its tag, over the document and the drawer, which stays open beneath
 * it out of sight, with no scrim over the viewer, so a tap on Back reaches Back. Back, or Escape, returns to the
 * document with the drawer where it was and that card focused. `name` shoots the viewer, then the document after.
 */
async function cardOpensViewerAndReturns(page: Page, testInfo: TestInfo, text: string, how: 'back' | 'escape', name: string) {
	const card = drawer(page).locator('.ct').filter({ hasText: text });
	await card.scrollIntoViewIfNeeded();
	const scrolled = await drawerList(page).evaluate((list) => list.scrollTop);
	await card.click();
	await expect(overlay(page)).toBeVisible();
	await expect(overlay(page).locator('[data-region-tag][aria-pressed="true"]:not([inert])')).toHaveCount(1);
	await expect(page.locator('[data-doc-comments-drawer]')).toBeHidden();
	// The viewer fits its image as the panes slide; its card is placed beside the tag once they stop.
	await settled(page);
	await shoot(page, testInfo, `${name}-viewer`, true);
	// Escape first closes the comment's own card in the viewer; on a phone its sheet pops its history entry afterwards.
	const layer = () => page.evaluate(() => (history.state as { backLayer?: number } | null)?.backLayer ?? null);
	const open = await layer();
	await page.keyboard.press('Escape');
	if ((page.viewportSize()?.width ?? 1440) <= 768 && open !== null) await expect.poll(layer).not.toBe(open);
	const back = bar(page).getByRole('button', { name: 'Back to walkthrough.md' });
	const box = (await back.boundingBox())!;
	const hit = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('button')?.getAttribute('aria-label') ?? null, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
	expect(hit, 'a tap on Back reaches Back').toBe('Back to walkthrough.md');
	if (how === 'back') await back.click();
	else await page.keyboard.press('Escape');
	await expect(overlay(page)).toHaveCount(0);
	await expect(card).toBeVisible();
	await expect.poll(() => card.evaluate((el) => el.contains(document.activeElement)), 'that card has focus').toBe(true);
	expect(await drawerList(page).evaluate((list) => list.scrollTop), 'the drawer list is where it was').toBe(scrolled);
	// The shell slides its panes back as the viewer leaves, and a sheet follows the document column frame by frame;
	// the frame is taken once the slide has ended and the sheet is back over that column.
	await settled(page);
	await shoot(page, testInfo, `${name}-back`, true);
}
const surface = (page: Page) => overlay(page).locator('[data-image-regions]');

async function openDocument(page: Page, seeded: Seeded, width: number) {
	await loginByUrl(page);
	if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(seeded.sessionTitle).first().click();
	await expect(docImage(page)).toBeVisible();
	await expect.poll(() => docImage(page).evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(NATURAL.width);
}
/** The closed document, opened again from the thread's Artifacts list. */
async function reopenDocument(page: Page) {
	await page.getByRole('button', { name: /^walkthrough\.md/ }).click();
	await expect(docImage(page)).toBeVisible();
}
/** Opens the document's image in the viewer and waits until it has drawn and fitted. */
async function openImage(page: Page, which = 0) {
	await page.locator('.cm-md-image img').nth(which).click();
	await expect(overlay(page)).toBeVisible();
	const img = overlay(page).locator('.frame img');
	await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(NATURAL.width);
	let last = '';
	await expect.poll(async () => {
		const now = JSON.stringify(await surface(page).boundingBox());
		const same = now === last;
		last = now;
		return same;
	}, { intervals: [150] }).toBe(true);
}
/** Draws a box over part of the image and posts a comment on it; the drag starts on bare image, not on a tag. */
async function commentOnRegion(page: Page, text: string, at = { x: 0.2, y: 0.25 }) {
	await bar(page).locator('[data-image-comment]').click();
	const box = (await surface(page).boundingBox())!;
	const from = { x: box.x + box.width * at.x, y: box.y + box.height * at.y };
	const to = { x: box.x + box.width * (at.x + 0.25), y: box.y + box.height * (at.y + 0.25) };
	// A press on a tag picks that comment rather than drawing, so a start on one would draw nothing.
	const under = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('.tag')?.getAttribute('aria-label') ?? null, from);
	expect(under, `the drag for "${text}" starts on bare image`).toBeNull();
	await drag(page, from, to);
	const composer = page.locator('textarea[placeholder="Comment…"]');
	await expect(composer).toBeVisible();
	await composer.fill(text);
	await page.keyboard.press('ControlOrMeta+Enter');
	await expect(composer).toHaveCount(0);
}
/** On a phone the posted comment stays open in a sheet over the bar; Done puts it away, as before a user's next tap. */
async function putSheetAway(page: Page) {
	if ((page.viewportSize()?.width ?? 1440) > 768) return;
	const backdrop = page.locator('[data-adaptive-drawer].drawer-backdrop');
	await expect(backdrop).toBeVisible();
	const layer = () => page.evaluate(() => (history.state as { backLayer?: number } | null)?.backLayer ?? null);
	const open = await layer();
	await page.getByRole('button', { name: 'Done', exact: true }).click();
	await expect(backdrop).toHaveCount(0);
	// The sheet pops its history entry asynchronously; a tap before that pop would be undone by it.
	if (open !== null) await expect.poll(layer, { message: 'the sheet\'s history entry is gone' }).not.toBe(open);
}
/** Every bar control is whole inside the bar, which stays one row inside the viewer. */
async function expectBarFits(page: Page) {
	const barBox = (await bar(page).boundingBox())!;
	const view = (await overlay(page).boundingBox())!;
	expect(barBox.x + barBox.width, 'the bar ends inside the viewer').toBeLessThanOrEqual(view.x + view.width + 0.5);
	expect(barBox.height, 'the bar is one row').toBeLessThan(100);
	for (const control of await bar(page).locator('button').all()) {
		if (!(await control.isVisible())) continue;
		const box = (await control.boundingBox())!;
		expect(box.x + box.width, `${await control.getAttribute('aria-label') ?? await control.innerText()} ends inside the bar`).toBeLessThanOrEqual(barBox.x + barBox.width + 0.5);
	}
	await expect(bar(page).getByRole('button', { name: 'Fit', exact: true })).toBeInViewport({ ratio: 1 });
}
const nextFrames = (page: Page) => page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
/**
 * Each margin marker sits at the top of its own image, in either theme, two frames after the theme changes: the rail
 * follows an image's size and place as it draws, with no waiting for things to settle. Leaving the viewer re-lays the
 * document out at its column's width first; that settles before the theme changes, so each switch is measured alone.
 */
async function expectMarkersBesideImages(page: Page, testInfo: TestInfo, name: string) {
	let last = '';
	await expect.poll(async () => {
		const now = JSON.stringify(await page.locator('.cm-md-image').evaluateAll((images) => images.map((image) => image.getBoundingClientRect().toJSON())));
		const same = now === last;
		last = now;
		return same;
	}, { intervals: [250] }).toBe(true);
	const start = await page.locator('html').getAttribute('data-theme');
	for (const scheme of start === 'dark' ? ['light', 'dark'] as const : ['dark', 'light'] as const) {
		await page.emulateMedia({ colorScheme: scheme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', scheme);
		await nextFrames(page);
		const markers = page.locator('.cm-comment-marker');
		for (let index = 0; index < await markers.count(); index++) {
			const image = (await page.locator('.cm-md-image').nth(index).boundingBox())!;
			const mark = (await markers.nth(index).boundingBox())!;
			expect(Math.abs(mark.y - image.y), `${scheme}: marker ${index + 1} at ${mark.y}, its image's top at ${image.y}`).toBeLessThanOrEqual(2);
		}
		await saveEvidenceScreenshot(page, testInfo, `${name}-${scheme}`, { fullPage: false });
	}
}
/** One painted frame: ms since sampling began, the drawer footer's send state, and the first comment card's top. */
type Sample = [number, 'pending' | 'sending' | 'gone', number];
/**
 * Reads, every frame until stopped, the document drawer footer's send state (Send now, sending, or gone) and where the
 * first comment card sits, as that frame paints them. The drawer eases its height from a resize observer, so each read
 * comes after the frame's resize observers; a read in a later task could see the footer already removed by the send's
 * answer, with the card at its final place for a frame never painted.
 */
async function startSampling(page: Page) {
	await installPaintedFrames(page);
	await page.evaluate(() => {
		const state = { seen: [] as Sample[], on: true };
		(window as unknown as { __footer: typeof state }).__footer = state;
		const start = performance.now();
		const measure = (): Sample => {
			const footer = document.querySelector<HTMLElement>('[data-drawer-unsent]');
			const card = document.querySelector('.canvas-comment-drawer .ct, .drawer-panel[role="dialog"] .ct');
			const status = !footer ? 'gone' : footer.querySelector('.btn')?.classList.contains('loading') ? 'sending' : 'pending';
			return [Math.round(performance.now() - start), status, Math.round((card?.getBoundingClientRect().top ?? 0) * 10) / 10];
		};
		const keep = (sample: Sample) => {
			state.seen.push(sample);
			return state.on;
		};
		(window as Window & { __eachPaintedFrame?: EachPaintedFrame }).__eachPaintedFrame!(measure, keep);
	});
}
const stopSampling = (page: Page) => page.evaluate(() => {
	const state = (window as unknown as { __footer: { seen: Sample[]; on: boolean } }).__footer;
	state.on = false;
	return state.seen;
});
/** The footer went from Send now, through sending, to gone, in that order. */
function expectSendStates(seen: Sample[], what: string) {
	const states = seen.map(([, state]) => state).filter((state, index, all) => index === 0 || state !== all[index - 1]);
	expect(states, `${what}: footer states`).toEqual(['pending', 'sending', 'gone']);
}
/** The card moved with the drawer from where it was to where it ends through in-between places, never more than half the way in a frame. */
function expectGlide(seen: Sample[], what: string) {
	const tops = seen.map(([, , top]) => top);
	const total = Math.abs(tops[tops.length - 1] - tops[0]);
	expect(total, `${what}: the drawer's height changed`).toBeGreaterThan(8);
	expect(new Set(tops).size, `${what}: in-between places ${JSON.stringify(tops)}`).toBeGreaterThanOrEqual(4);
	// A frame the browser dropped under load (several times the usual gap) covers the motion of the frames it skipped, so
	// only frames at the usual pace count as one frame's move.
	const gaps = seen.slice(1).map(([at], index) => at - seen[index][0]);
	const usual = [...gaps].sort((a, b) => a - b)[Math.floor(gaps.length / 2)];
	const moves = tops.slice(1).map((top, index) => (gaps[index] > usual * 3 ? 0 : Math.abs(top - tops[index])));
	expect(Math.max(...moves), `${what}: largest move in one frame of ${total}`).toBeLessThanOrEqual(total / 2);
}
const mockOnly = (testInfo: TestInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
};

test.describe('@flow:image-region-comments @surface:documents comments on images inside a document', () => {
	test('refreshes a replaced image in an open document and keeps its region comment', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(180_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		const seeded = await seedDocument(page, request, 'doc-image-live-refresh', null, LIVE_REFRESH_DOC);
		const imagePath = seeded.imagePath.replace(/\.png$/, '.svg');
		fs.writeFileSync(imagePath, svgImage(NATURAL));
		await openDocument(page, seeded, 1440);
		await openImage(page);
		await commentOnRegion(page, 'Image replaced while this document stays open');
		await page.keyboard.press('Escape');
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);
		await expect(marker(page)).toBeVisible();

		const replaced = await request.put(`/api/files/content${query}`, {
			data: { path: imagePath, text: svgImage(REPLACED_NATURAL), thread_id: seeded.sessionId },
		});
		expect(replaced.ok(), `image replace failed: ${replaced.status()}`).toBeTruthy();
		await expect.poll(() => docImage(page).evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(REPLACED_NATURAL.width);
		await expect(marker(page)).toBeVisible();
	});

	test('reopens a replaced image and keeps its region comment on the fresh image', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(180_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		const seeded = await seedDocument(page, request, 'doc-image-reopen', null);
		await openDocument(page, seeded, 1440);
		await openImage(page);
		await commentOnRegion(page, 'Image replaced after this comment');
		await page.keyboard.press('Escape');
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);
		await expect(marker(page)).toBeVisible();

		fs.writeFileSync(seeded.imagePath, syntheticPng(REPLACED_NATURAL.width, REPLACED_NATURAL.height));
		await page.getByRole('button', { name: 'Close walkthrough.md' }).click();
		await expect(page.getByRole('button', { name: 'Close walkthrough.md' })).toHaveCount(0);
		await reopenDocument(page);
		await expect.poll(() => docImage(page).evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(REPLACED_NATURAL.width);
		await expect(marker(page)).toBeVisible();

		await marker(page).click();
		await expect(overlay(page)).toBeVisible();
		await expect.poll(() => overlay(page).locator('.frame img').evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(REPLACED_NATURAL.width);
		await expect(tag(page, 1)).toBeVisible();
		await expect(tag(page, 1)).toHaveAttribute('aria-label', /image changed since this comment/);
	});

	test('comment on part of a document image, find it again in the document, and send from its drawer (1440)', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(180_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		const seeded = await seedDocument(page, request, 'doc-image-regions', null);
		await openDocument(page, seeded, 1440);
		await shoot(page, testInfo, 'doc-1440-01-document-image', true);

		// J1/J2: the image opens over the document; a box drawn on it becomes a numbered region comment.
		await openImage(page);
		await expect(bar(page).getByRole('button', { name: 'Back to walkthrough.md' })).toBeVisible();
		await expect(viewerSend(page)).toHaveCount(0);
		await shoot(page, testInfo, 'doc-1440-02-viewer-open', true);
		await commentOnRegion(page, 'The header crowds the logo');
		await expect(tag(page, 1)).toBeVisible();
		// A person's comment is delivered as it is made: nothing waits to be sent until a re-opened comment does.
		await expect(bar(page).locator('[data-image-comments-pill]')).not.toHaveAttribute('aria-label', /not sent yet/);
		await leaveUserCommentsUnsent(request, seeded.artifactId);
		// RSR-10: the viewer over a document has no Send; its pill marks the document's unsent message.
		await expect(viewerSend(page)).toHaveCount(0);
		await expect(bar(page).locator('[data-image-comments-pill]')).toHaveAttribute('aria-label', unsentName(1));
		await page.keyboard.press('Escape');
		await expectBarFits(page);
		await shoot(page, testInfo, 'doc-1440-03-viewer-pending', true);

		// Back is not leaving: the document's own row shows the same pending comment, and the image its margin marker.
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);
		await expect(docComments(page)).toHaveAttribute('aria-label', unsentName(1));
		await expect(marker(page)).toBeVisible();
		await expectMarkersBesideImages(page, testInfo, 'doc-1440-04-document-marker-pending');

		// J3: the marker opens the viewer on its comment, its card at its tag.
		await marker(page).click();
		await expect(overlay(page)).toBeVisible();
		await expect(tag(page, 1)).toHaveAttribute('aria-pressed', 'true');
		await expect(page.locator('[data-popover]')).toContainText('The header crowds the logo');
		await shoot(page, testInfo, 'doc-1440-05-marker-opens-card', true);
		await page.keyboard.press('Escape');
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);

		// FR13.6: the document's drawer groups the image's comments under its thumbnail; a card opens the viewer at its tag.
		await docComments(page).click();
		const card = page.locator('.canvas-comment-drawer .ct').filter({ hasText: 'The header crowds the logo' });
		await expect(card).toBeVisible();
		await expect(drawerThumb(page)).toBeVisible();
		await expect(drawer(page).locator('[data-drawer-unsent]')).toContainText('1 message not sent yet');
		await shoot(page, testInfo, 'doc-1440-06-drawer-image-group', true);
		await cardOpensViewerAndReturns(page, testInfo, 'The header crowds the logo', 'back', 'doc-1440-06b-drawer-card');

		// Send now in the drawer's footer closes the document's batch, and nothing is left to send in either place.
		await expect(footerSend(page)).toBeInViewport({ ratio: 1 });
		const sent = await sendFromDrawerByKeyboard(page, testInfo, docImage(page), 'doc-1440-06c-drawer-sent-focus', async () => {
			// The document's row says what was sent, and says it for 1.5 s only: check it as the footer goes.
			await expect(docDelivery(page)).toContainText(/1 comment sent/);
			await saveEvidenceScreenshot(page, testInfo, 'doc-1440-07-drawer-sent-confirmed-dark', { fullPage: false });
		});
		const body = sent.postDataJSON() as { artifact_id?: string } | null;
		if (body?.artifact_id) expect(body.artifact_id).toBe(seeded.artifactId);
		await expect(docComments(page)).not.toHaveAttribute('aria-label', /not sent yet/);
		await shoot(page, testInfo, 'doc-1440-08-document-sent', true);
	});

	test('on a phone the viewer bar keeps Back, Fit, Comment and the count, and the batch is sent from the document drawer (393)', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(180_000);
		await page.setViewportSize({ width: 393, height: 852 });
		const seeded = await seedDocument(page, request, 'doc-image-regions-phone', null);
		await openDocument(page, seeded, 393);
		await shoot(page, testInfo, 'doc-393-01-document-image', true);
		await openImage(page);
		await expect(viewerSend(page)).toHaveCount(0);
		// On a phone Back shows only "Back"; its name still says where it goes.
		await expect(bar(page).getByRole('button', { name: 'Back to walkthrough.md' })).toHaveText(/^\s*Back\s*$/);
		await expect(bar(page).getByRole('button', { name: 'Fit', exact: true })).toBeVisible();
		await expect(bar(page).locator('[data-image-comment]')).toBeVisible();
		await expect(bar(page).locator('[data-image-comments-pill]')).toBeVisible();
		await shoot(page, testInfo, 'doc-393-02-viewer-open', true);

		await commentOnRegion(page, 'Tap target too small');
		await leaveUserCommentsUnsent(request, seeded.artifactId);
		// RSR-10: no Send in the viewer while a message is pending; the pill marks it.
		await expect(viewerSend(page)).toHaveCount(0);
		await expect(bar(page).locator('[data-image-comments-pill]')).toHaveAttribute('aria-label', unsentName(1));
		await page.keyboard.press('Escape');
		// Every bar control keeps a 44 px target and the bar stays one row inside the screen.
		for (const control of await bar(page).locator('button').all()) {
			if (!(await control.isVisible())) continue;
			expect((await control.boundingBox())!.height, await control.innerText()).toBeGreaterThanOrEqual(44);
		}
		await expectBarFits(page);
		await shoot(page, testInfo, 'doc-393-03-viewer-pending', true);

		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(docComments(page)).toHaveAttribute('aria-label', unsentName(1));
		await expectMarkersBesideImages(page, testInfo, 'doc-393-04-document-marker-pending');
		await docComments(page).click();
		await expect(page.locator('.drawer-panel[role="dialog"], .canvas-comment-drawer').getByText('Tap target too small')).toBeVisible();
		await expect(drawerThumb(page)).toBeVisible();
		await expect(drawer(page).locator('[data-drawer-unsent]')).toContainText('1 message not sent yet');
		await shoot(page, testInfo, 'doc-393-05-drawer-image-group', true);
		await cardOpensViewerAndReturns(page, testInfo, 'Tap target too small', 'escape', 'doc-393-05b-drawer-card');

		// Send now in the footer, whole inside the screen, sends the batch; the footer goes. (Its 44 px target under a
		// coarse pointer is doc-send-on-close's phone case.)
		await expect(footerSend(page)).toBeInViewport({ ratio: 1 });
		await sendFromDrawerByKeyboard(page, testInfo, docImage(page), 'doc-393-05c-drawer-sent-focus');
		await expect(docComments(page)).not.toHaveAttribute('aria-label', /not sent yet/);
		await shoot(page, testInfo, 'doc-393-06-document-sent', true);
	});

	for (const width of [1440, 393]) test(`many comments on two images: tags in the viewer, a marker per image, a drawer group per image (${width})`, async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(240_000);
		await page.setViewportSize({ width, height: width > 768 ? 900 : 852 });
		const seeded = await seedDocument(page, request, `doc-image-regions-many-${width}`, null, DOC_TWO);
		await openDocument(page, seeded, width);
		const notes = [
			// Spaced so that on a phone each new box starts clear of the tags already placed.
			['The header crowds the logo', { x: 0.05, y: 0.05 }], ['Search needs a label', { x: 0.6, y: 0.1 }],
			['This card is too tall', { x: 0.1, y: 0.55 }], ['Footer links are low contrast', { x: 0.6, y: 0.6 }],
		] as const;
		await openImage(page, 0);
		for (const [text, at] of notes) { await commentOnRegion(page, text, at); await putSheetAway(page); }
		// The last card is still open on a desktop; on a phone Done put it away, and Escape would leave the viewer.
		if (width > 768) await page.keyboard.press('Escape');
		await expect(tag(page, 4)).toBeVisible();
		await shoot(page, testInfo, `doc-${width}-many-01-viewer-four-tags`, true);
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);

		await openImage(page, 1);
		await commentOnRegion(page, 'Toggle labels are unclear', { x: 0.1, y: 0.2 });
		await putSheetAway(page);
		await commentOnRegion(page, 'Save should be primary', { x: 0.6, y: 0.65 });
		await putSheetAway(page);
		if (width > 768) await page.keyboard.press('Escape');
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);
		// A marker beside each image, and the document's row counting all six.
		await expect(page.locator('.cm-comment-marker')).toHaveCount(2);
		await leaveUserCommentsUnsent(request, seeded.artifactId);
		await expect(docComments(page)).toHaveAttribute('aria-label', unsentName(6));
		await expectMarkersBesideImages(page, testInfo, `doc-${width}-many-02-document-two-markers`);

		// The drawer: one group per image, each under one thumbnail, comments in number order; scrolled to the second.
		await docComments(page).click();
		const thumbs = drawer(page).locator('[data-region-thumb]');
		await expect(thumbs).toHaveCount(2);
		await shoot(page, testInfo, `doc-${width}-many-03-drawer-two-groups`, true);
		await thumbs.nth(1).scrollIntoViewIfNeeded();
		await expect(drawer(page).getByText('Save should be primary')).toBeVisible();
		// The frame is named "scrolled": the list has actually moved, with the first group's thumbnail above the fold.
		await expect.poll(() => drawer(page).locator('.drawer-comments').evaluate((list) => list.scrollTop), 'the drawer list scrolled').toBeGreaterThan(0);
		await shoot(page, testInfo, `doc-${width}-many-04-drawer-scrolled`, true);
		// From the scrolled drawer a card opens its image, and the way back (Escape here, Back in the single-image journey) finds the drawer where it was.
		await cardOpensViewerAndReturns(page, testInfo, 'Save should be primary', width > 768 ? 'escape' : 'back', `doc-${width}-many-05-scrolled-card`);
	});

	for (const width of [1440, 393]) test(`the document drawer's footer says when unsent messages go, and a batch not delivered with Retry (${width})`, async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(180_000);
		await page.setViewportSize({ width, height: width > 768 ? 900 : 852 });
		const seeded = await seedDocument(page, request, `doc-image-regions-states-${width}`, null);
		// A failed delivery sets this; the frames rewrite the server's answer to show it.
		const inject: { undelivered?: unknown[] } = {};
		await page.route('**/api/artifacts/*/comments*', async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const response = await route.fetch();
			const json = await response.json();
			if (json.feedback && inject.undelivered) json.feedback.undelivered_batches = inject.undelivered;
			await route.fulfill({ response, json });
		});
		await openDocument(page, seeded, width);
		await openImage(page);
		await commentOnRegion(page, 'The header crowds the logo');
		await leaveUserCommentsUnsent(request, seeded.artifactId);
		await page.keyboard.press('Escape');
		// RSR-10: the viewer shows no Send; the document's drawer footer does.
		await expect(viewerSend(page)).toHaveCount(0);
		await expect(bar(page).locator('.sending-now')).toHaveCount(0);
		await expectBarFits(page);
		await backToDocumentDrawer(page);
		await expect(footer(page)).toContainText(/1 message not sent yet · it goes to .+ in a few seconds\./);
		await expect(footerSend(page)).toBeInViewport({ ratio: 1 });
		await shoot(page, testInfo, `doc-${width}-09-drawer-unsent`, true);

		inject.undelivered = [{ id: 'batch-1', item_count: 1, undelivered_reason: 'owner_unavailable', attempts: 1 }];
		await page.route('**/api/doc-feedback/send', (route) => route.fulfill({ json: { woken: [], undelivered_batches: inject.undelivered } }));
		await footerSend(page).click();
		// Not delivered: the document's own row offers Retry and claims nothing was sent.
		await closeDocumentDrawer(page);
		const retry = docDelivery(page).getByRole('button', { name: /Retry/ });
		await expect(retry).toBeVisible();
		await expect(docDelivery(page)).not.toContainText(/comments? sent/);
		await expect(retry).toBeInViewport({ ratio: 1 });
		if (width <= 768) expect((await retry.boundingBox())!.height, 'Retry keeps a 44 px target').toBeGreaterThanOrEqual(44);
		// The viewer over the document still offers no Retry of its own.
		await openImage(page);
		await expect(bar(page).getByRole('button', { name: /Retry/ })).toHaveCount(0);
		await shoot(page, testInfo, `doc-${width}-10-document-undelivered-retry`, true);
	});

	for (const [label, width, reduced] of [['1440', 1440, false], ['393', 393, false], ['1440-reduced-motion', 1440, true]] as const) {
		test(`Send now in the document drawer's footer: sending, sent, then the footer leaves with nothing jumping (recorded, ${label})`, async ({ browser, request }, testInfo) => {
			mockOnly(testInfo);
			test.setTimeout(180_000);
			const size = { width, height: width > 768 ? 900 : 852 };
			const context = await browser.newContext({
				baseURL: testInfo.project.use.baseURL, viewport: size, colorScheme: 'dark',
				reducedMotion: reduced ? 'reduce' : 'no-preference', recordVideo: { dir: testInfo.outputPath('video'), size },
			});
			const page = await context.newPage();
			// The page's animations run ten times slower from before the send to the end, so the recording and the per-frame
			// samples resolve any motion; the send itself is held for a moment so its sending state is on screen.
			const cdp = await context.newCDPSession(page);
			const slow = async () => { await cdp.send('Animation.enable'); await cdp.send('Animation.setPlaybackRate', { playbackRate: 0.1 }); };
			try {
				const seeded = await seedDocument(page, request, `doc-send-motion-${label}`, null);
				await openDocument(page, seeded, width);
				await openImage(page);
				await commentOnRegion(page, 'The header crowds the logo');
				await leaveUserCommentsUnsent(request, seeded.artifactId);
				await putSheetAway(page);
				if (width > 768) await page.keyboard.press('Escape');
				// RSR-10: nothing to send from the viewer; the document drawer's footer is where it goes.
				await expect(viewerSend(page)).toHaveCount(0);
				await backToDocumentDrawer(page);
				await expect(footerSend(page)).toBeVisible();
				await page.route('**/api/doc-feedback/send', async (route) => { await new Promise((resolve) => setTimeout(resolve, 800)); await route.fallback(); });
				await slow();
				await startSampling(page);
				await footerSend(page).click();
				await expect(footer(page)).toHaveCount(0, { timeout: 15_000 });
				// Sent: the document's own row says so once the footer has gone.
				await expect(docDelivery(page)).toContainText(/1 comment sent/);
				await page.waitForTimeout(300);
				const seen = await stopSampling(page);
				fs.writeFileSync(testInfo.outputPath('samples.json'), JSON.stringify({ playbackRate: 0.1, seen }));

				expectSendStates(seen, label);
				// The drawer gives back the footer's height as it leaves: eased with motion, a single cut under reduced
				// motion (which crossfades, nothing slides). The frames before the footer left are only the sending state.
				const leaving = seen.slice(seen.findIndex(([, state]) => state === 'gone') - 1);
				if (reduced) expect(new Set(leaving.map(([, , top]) => top)).size, `one cut ${JSON.stringify(leaving)}`).toBeLessThanOrEqual(2);
				else expectGlide(leaving, label);
			} finally {
				await cdp.send('Animation.setPlaybackRate', { playbackRate: 1 }).catch(() => {});
				const video = page.video();
				await context.close();
				await video?.saveAs(testInfo.outputPath(`send-motion-${label}.webm`));
			}
		});
	}
});
