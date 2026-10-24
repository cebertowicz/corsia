import fs from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';
import { syntheticPng } from '../support/syntheticImages';
import { drag, settled, shoot } from '../support/imageRegionComments';
import { headerSend, leaveUserCommentsUnsent, sendByKeyboard, watchSendRequests, turnOffDocumentComments } from '../support/heldComments';
import { installPaintedFrames, type EachPaintedFrame } from '../support/paintedFrames';
import { expectBarFits } from '../support/docImageComments';

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
/** The document's pending batch, as its header pill names it; its Send is the document header's. */
const unsentName = (count: number) => new RegExp(`, ${count} messages? not sent yet$`);
const drawerThumb = (page: Page) => drawer(page).locator('[data-region-thumb]').first();
/** The document's own delivery row: what was sent, or not delivered with Retry. */
const docDelivery = (page: Page) => page.locator('.document-header .feedback-controls');
/** Back from the viewer to the document, then its comments drawer opened from the header pill. */
async function backToDocumentDrawer(page: Page) {
	await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
	await expect(overlay(page)).toHaveCount(0);
	await docComments(page).click();
	await expect(drawer(page)).toBeVisible();
	await expect(page.locator('[data-drawer-unsent]'), 'the drawer has no send footer').toHaveCount(0);
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
	await turnOffDocumentComments(page);
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
/** One painted frame: ms since sampling began, the header Send's state, and the editor's top. */
type Sample = [number, 'pending' | 'sending' | 'sent' | 'collapsing' | 'gone', number];
/**
 * Reads, every frame until stopped, the document header Send's state (Send, sending, Sent, or gone) and where the
 * editor's content sits, as that frame paints them: a Send that arrives or leaves must not move the document.
 */
async function startSampling(page: Page) {
	await installPaintedFrames(page);
	await page.evaluate(() => {
		const state = { seen: [] as Sample[], on: true };
		(window as unknown as { __sender: typeof state }).__sender = state;
		const start = performance.now();
		const measure = (): Sample => {
			const slot = document.querySelector<HTMLElement>('.document-header .sending-now');
			const button = slot?.querySelector('.btn');
			const collapsing = document.querySelector('.document-header .document-notices.empty');
			const status = !slot ? 'gone' : collapsing ? 'collapsing' : !button ? 'sent' : button.classList.contains('loading') ? 'sending' : 'pending';
			return [Math.round(performance.now() - start), status, Math.round((document.querySelector('.cm-content')?.getBoundingClientRect().top ?? 0) * 10) / 10];
		};
		const keep = (sample: Sample) => {
			state.seen.push(sample);
			return state.on;
		};
		(window as Window & { __eachPaintedFrame?: EachPaintedFrame }).__eachPaintedFrame!(measure, keep);
	});
}
const stopSampling = (page: Page) => page.evaluate(() => {
	const state = (window as unknown as { __sender: { seen: Sample[]; on: boolean } }).__sender;
	state.on = false;
	return state.seen;
});
/** A successful Send holds its confirmation, then collapses its empty row and leaves. */
function expectSendStates(seen: Sample[], what: string) {
	const states = seen.map(([, state]) => state).filter((state, index, all) => index === 0 || state !== all[index - 1]);
	expect(states, `${what}: send states`).toEqual(['pending', 'sending', 'sent', 'collapsing', 'gone']);
}
/** Stable through confirmation; afterward the row releases its space monotonically, without a permanent gap. */
function expectStill(seen: Sample[], what: string, reduced: boolean) {
	const active = seen.filter(([, state]) => ['pending', 'sending', 'sent'].includes(state)).map(([, , top]) => top);
	expect(Math.max(...active) - Math.min(...active), `${what}: active confirmation stays still`).toBeLessThanOrEqual(1);
	const leaving = seen.filter(([, state]) => state === 'collapsing' || state === 'gone').map(([, , top]) => top);
	const from = active[0], to = leaving.at(-1)!;
	expect(to, `${what}: the empty row releases its space`).toBeLessThan(from);
	for (let index = 1; index < leaving.length; index++) expect(leaving[index], `${what}: collapse is monotonic`).toBeLessThanOrEqual(leaving[index - 1] + 0.2);
	if (!reduced) expect(new Set(leaving.filter((top) => top < from - 0.5 && top > to + 0.5)).size, `${what}: collapse has painted intermediate positions`).toBeGreaterThanOrEqual(3);
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
		// The comment is held for the image: the bar's Send is its one sender, and the document's header has none for it.
		await expect(viewerSend(page)).toHaveText('Send 1 comment');
		await page.keyboard.press('Escape');
		await expectBarFits(page);
		await shoot(page, testInfo, 'doc-1440-03-viewer-pending', true);

		// Back leaves the image: its held comment goes once, and the document's own row has nothing waiting.
		const sends = watchSendRequests(page);
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);
		await expect.poll(() => sends.document).toHaveLength(1);
		expect(JSON.parse(sends.document[0]).delivery_policy).toBe('image-held');
		await expect(docComments(page)).not.toHaveAttribute('aria-label', /not sent yet/);
		await expect(headerSend(page)).toHaveCount(0);
		await expect(marker(page)).toBeVisible();
		await expectMarkersBesideImages(page, testInfo, 'doc-1440-04-document-marker');

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
		await expect(drawer(page).locator('[data-drawer-unsent]')).toHaveCount(0);
		await shoot(page, testInfo, 'doc-1440-06-drawer-image-group', true);
		await cardOpensViewerAndReturns(page, testInfo, 'The header crowds the logo', 'back', 'doc-1440-06b-drawer-card');

		// A re-opened comment is held for the document's owner: the header's Send is its one sender, from the keyboard.
		await closeDocumentDrawer(page);
		await leaveUserCommentsUnsent(request, seeded.artifactId);
		await expect(docComments(page)).toHaveAttribute('aria-label', unsentName(1));
		await expect(headerSend(page)).toHaveText('Send 1 comment');
		await expect(headerSend(page)).toBeInViewport({ ratio: 1 });
		await shoot(page, testInfo, 'doc-1440-06c-header-send', true);
		const sent = await sendByKeyboard(page, headerSend(page), async () => {
			// The document's row says what was sent, and says it for 1.5 s only: check it as the Send goes.
			await expect(docDelivery(page)).toContainText(/1 comment sent/);
			await saveEvidenceScreenshot(page, testInfo, 'doc-1440-07-header-sent-confirmed-dark', { fullPage: false });
		});
		const body = sent.postDataJSON() as { artifact_id?: string; delivery_policy?: string } | null;
		if (body?.artifact_id) expect(body.artifact_id).toBe(seeded.artifactId);
		expect(body?.delivery_policy).toBe('document-held');
		await expect(docComments(page)).not.toHaveAttribute('aria-label', /not sent yet/);
		await shoot(page, testInfo, 'doc-1440-08-document-sent', true);
	});

	test('on a phone the viewer bar keeps Back, Fit, Comment and the count with its own Send, and the document header sends the rest (393)', async ({ page, request }, testInfo) => {
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
		// The comment is held for the image: the bar's Send is its one sender.
		await expect(viewerSend(page)).toHaveText('Send 1 comment');
		await page.keyboard.press('Escape');
		// Every bar control keeps a 44 px target; the comments pill can reflow inside the screen.
		for (const control of await bar(page).locator('button').all()) {
			if (!(await control.isVisible())) continue;
			expect((await control.boundingBox())!.height, await control.innerText()).toBeGreaterThanOrEqual(44);
		}
		await expectBarFits(page);
		await shoot(page, testInfo, 'doc-393-03-viewer-pending', true);

		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);
		await expect(docComments(page)).not.toHaveAttribute('aria-label', /not sent yet/);
		await expectMarkersBesideImages(page, testInfo, 'doc-393-04-document-marker');
		await docComments(page).click();
		await expect(page.locator('.drawer-panel[role="dialog"], .canvas-comment-drawer').getByText('Tap target too small')).toBeVisible();
		await expect(drawerThumb(page)).toBeVisible();
		await expect(drawer(page).locator('[data-drawer-unsent]')).toHaveCount(0);
		await shoot(page, testInfo, 'doc-393-05-drawer-image-group', true);
		await cardOpensViewerAndReturns(page, testInfo, 'Tap target too small', 'escape', 'doc-393-05b-drawer-card');

		// A re-opened comment is held for the owner: the header's Send, whole inside the screen, sends it.
		await closeDocumentDrawer(page);
		await leaveUserCommentsUnsent(request, seeded.artifactId);
		await expect(headerSend(page)).toHaveText('Send 1 comment');
		await expect(headerSend(page)).toBeInViewport({ ratio: 1 });
		await shoot(page, testInfo, 'doc-393-05c-header-send', true);
		await sendByKeyboard(page, headerSend(page));
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

	for (const width of [1440, 393]) test(`the header's Send is whole, and a batch not delivered shows Retry (${width})`, async ({ page, request }, testInfo) => {
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
		await page.keyboard.press('Escape');
		// The image's own Send sits in the viewer bar; the document's header shows none for it.
		await expect(viewerSend(page)).toHaveText('Send 1 comment');
		await expectBarFits(page);
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);
		await leaveUserCommentsUnsent(request, seeded.artifactId);
		await expect(headerSend(page)).toHaveText('Send 1 comment');
		await expect(headerSend(page)).toBeInViewport({ ratio: 1 });
		await shoot(page, testInfo, `doc-${width}-09-header-send`, true);

		inject.undelivered = [{ id: 'batch-1', item_count: 1, undelivered_reason: 'owner_unavailable', attempts: 1 }];
		await page.route('**/api/doc-feedback/send', (route) => route.fulfill({ json: { woken: [], undelivered_batches: inject.undelivered } }));
		await headerSend(page).click();
		// Not delivered: the document's own row offers Retry and claims nothing was sent.
		const retry = docDelivery(page).getByRole('button', { name: /Retry/ });
		await expect(retry).toBeVisible();
		await expect(docDelivery(page)).not.toContainText(/comments? sent/);
		await expect(retry).toBeInViewport({ ratio: 1 });
		if (width <= 768) expect((await retry.boundingBox())!.height, 'Retry keeps a 44 px target').toBeGreaterThanOrEqual(44);
		await shoot(page, testInfo, `doc-${width}-10-document-undelivered-retry`, true);
		await openImage(page);
		await expect(bar(page).getByRole('button', { name: /Retry/ })).toHaveCount(0);
	});

	for (const [label, width, reduced] of [['1440', 1440, false], ['393', 393, false], ['1440-reduced-motion', 1440, true]] as const) {
		test(`the header's Send: sending, sent, then it leaves with nothing jumping (recorded, ${label})`, async ({ browser, request }, testInfo) => {
			mockOnly(testInfo);
			test.setTimeout(180_000);
			const size = { width, height: width > 768 ? 900 : 852 };
			const context = await browser.newContext({
				baseURL: testInfo.project.use.baseURL, viewport: size, colorScheme: 'dark',
				reducedMotion: reduced ? 'reduce' : 'no-preference', recordVideo: { dir: testInfo.outputPath('video'), size },
			});
			const page = await context.newPage();
			const captureCollapseFrame = async (name: string) => {
				const output = process.env.HELD_COMMENT_DELIVERY_FRAMES_OUT;
				if (!output || width !== 393) return;
				// Still frames show the normal painted state, not the recording's deliberately slowed entrance.
				await cdp.send('Animation.setPlaybackRate', { playbackRate: 1 });
				await page.locator('.document-notices').evaluate(async (element) => {
					await Promise.all(element.getAnimations({ subtree: true }).map((animation) => animation.finished.catch(() => undefined)));
				});
				for (const colorScheme of ['light', 'dark'] as const) {
					await page.emulateMedia({ colorScheme });
					await expect(page.locator('html')).toHaveAttribute('data-theme', colorScheme);
					await page.screenshot({ path: path.join(output, `${name}-393-${colorScheme}.png`), fullPage: false });
				}
				await cdp.send('Animation.setPlaybackRate', { playbackRate: 0.1 });
			};
			// The page's animations run ten times slower from before the send to the end, so the recording and the per-frame
			// samples resolve any motion; the send itself is held for a moment so its sending state is on screen.
			const cdp = await context.newCDPSession(page);
			const slow = async () => { await cdp.send('Animation.enable'); await cdp.send('Animation.setPlaybackRate', { playbackRate: 0.1 }); };
			try {
				const seeded = await seedDocument(page, request, `doc-send-motion-${label}`, null);
				await openDocument(page, seeded, width);
				await openImage(page);
				await commentOnRegion(page, 'The header crowds the logo');
				await putSheetAway(page);
				if (width > 768) await page.keyboard.press('Escape');
				await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
				await expect(overlay(page)).toHaveCount(0);
				await leaveUserCommentsUnsent(request, seeded.artifactId);
				await expect(headerSend(page)).toBeVisible();
				await page.route('**/api/doc-feedback/send', async (route) => { await new Promise((resolve) => setTimeout(resolve, 800)); await route.fallback(); });
				// Finish the setup's Send entrance before slowing and filming this send's lifecycle.
				await page.locator('.document-header').evaluate(async (element) => {
					await Promise.all(element.getAnimations({ subtree: true }).map((animation) => animation.finished.catch(() => undefined)));
				});
				await slow();
				await startSampling(page);
				await headerSend(page).click();
				await expect(headerSend(page)).toHaveCount(0, { timeout: 15_000 });
				// Sent: the document's own row says so once the Send has gone.
				await expect(docDelivery(page)).toContainText(/1 comment sent/);
				await captureCollapseFrame('header-sent-before-collapse');
				// Keep filming until Sent's hold and the slowed exit finish, including a painted frame after it leaves.
				await expect.poll(() => page.evaluate(() => {
					const seen = (window as unknown as { __sender: { seen: Sample[] } }).__sender.seen;
					return seen.at(-1)?.[1];
				}), { timeout: 15_000 }).toBe('gone');
				const seen = await stopSampling(page);
				fs.writeFileSync(testInfo.outputPath('samples.json'), JSON.stringify({ playbackRate: 0.1, seen }));

				expectSendStates(seen, label);
				expectStill(seen, label, reduced);
				expect((await page.locator('.document-notices').boundingBox())!.height, 'zero leaves no reserved notice row').toBe(0);
				await expect(page.locator('.document-notices')).toHaveAttribute('inert', '');
				await captureCollapseFrame('header-after-collapse');
			} finally {
				await cdp.send('Animation.setPlaybackRate', { playbackRate: 1 }).catch(() => {});
				const video = page.video();
				await context.close();
				await video?.saveAs(testInfo.outputPath(`send-motion-${label}.webm`));
			}
		});
	}
});
