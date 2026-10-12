import fs from 'node:fs';
import path from 'node:path';
import { expect, test, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';
import { syntheticPng } from '../support/syntheticImages';
import { drag, shoot } from '../support/imageRegionComments';

// Comments on parts of an image inside a document (DCOL-FR13.3, 13.5, 13.6, 13.12): the viewer over the document,
// the margin marker and drawer group back in it, and the one feedback batch both show and send.

const NATURAL = { width: 1600, height: 1000 };
const filler = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => `Paragraph ${from + i}: the walkthrough continues with enough words to wrap across a few lines of the document column.\n`).join('\n');
const DOC = ['# Design walkthrough', '', filler(1, 4), '![Mock screen](shots/screen.png)', '', filler(4, 12)].join('\n');
/** Two images, for many comments on each and a drawer with more than one image group. */
const DOC_TWO = ['# Design walkthrough', '', filler(1, 2), '![Mock screen](shots/screen.png)', '', filler(2, 4), '![Settings screen](shots/settings.png)', '', filler(4, 12)].join('\n');
const query = `?token=${encodeURIComponent(TEST_TOKEN)}`;

interface Seeded { sessionTitle: string; artifactId: string }

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
	return { sessionTitle, artifactId };
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
const surface = (page: Page) => overlay(page).locator('[data-image-regions]');

async function openDocument(page: Page, seeded: Seeded, width: number) {
	await loginByUrl(page);
	if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(seeded.sessionTitle).first().click();
	await expect(docImage(page)).toBeVisible();
	await expect.poll(() => docImage(page).evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(NATURAL.width);
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
/** One frame: ms since sampling began, the count pill's left edge, and the Send slot's width and opacity (null: no slot). */
type Sample = [number, number, number | null, number | null];
/** The slot's own motion as it started: its first and last keyframes and its duration at the page's real speed. */
type SlotMotion = { from: { maxWidth: string; opacity: number }; to: { maxWidth: string; opacity: number }; duration: number };
/**
 * Reads, every frame until stopped, where the bar's count pill sits and the Send slot's size and opacity, and each motion
 * the slot runs that opens or closes its space (Svelte's own timing animations carry no keyframes, so they are skipped).
 */
async function startSampling(page: Page) {
	await page.evaluate(() => {
		const state = { seen: [] as Sample[], motions: [] as SlotMotion[], on: true };
		const known = new WeakSet<Animation>();
		(window as unknown as { __slot: typeof state }).__slot = state;
		const start = performance.now();
		const frame = (keyframe: ComputedKeyframe) => ({ maxWidth: String(keyframe.maxWidth), opacity: Number(keyframe.opacity) });
		const tick = () => {
			const pill = document.querySelector('.image-overlay .bar [data-image-comments-pill]');
			const slot = document.querySelector<HTMLElement>('.image-overlay .bar .sending-now');
			for (const animation of slot?.getAnimations() ?? []) {
				const effect = animation.effect as KeyframeEffect | null;
				const keyframes = effect?.getKeyframes() ?? [];
				if (known.has(animation) || !keyframes.some((keyframe) => 'maxWidth' in keyframe)) continue;
				known.add(animation);
				state.motions.push({ from: frame(keyframes[0]), to: frame(keyframes.at(-1)!), duration: Number(effect!.getTiming().duration) });
			}
			const round = (value: number) => Math.round(value * 10) / 10;
			state.seen.push([Math.round(performance.now() - start), round(pill?.getBoundingClientRect().left ?? 0),
				slot ? round(slot.getBoundingClientRect().width) : null, slot ? Number(getComputedStyle(slot).opacity) : null]);
			if (state.on) requestAnimationFrame(tick);
		};
		requestAnimationFrame(tick);
	});
}
const stopSampling = (page: Page) => page.evaluate(() => {
	const state = (window as unknown as { __slot: { seen: Sample[]; motions: SlotMotion[]; on: boolean } }).__slot;
	state.on = false;
	return { seen: state.seen, motions: state.motions };
});
/** The pill moved from where it was to where it ends through in-between places, never more than half the way in a frame. */
function expectGlide(seen: Sample[], what: string) {
	const xs = seen.map(([, x]) => x);
	const total = Math.abs(xs[xs.length - 1] - xs[0]);
	expect(total, `${what}: the pill moved`).toBeGreaterThan(8);
	expect(new Set(xs).size, `${what}: in-between places ${JSON.stringify(xs)}`).toBeGreaterThanOrEqual(4);
	const jump = Math.max(...xs.slice(1).map((x, index) => Math.abs(x - xs[index])));
	expect(jump, `${what}: largest move in one frame of ${total}`).toBeLessThanOrEqual(total / 2);
}
const mockOnly = (testInfo: TestInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
};

test.describe('@flow:image-region-comments @surface:documents comments on images inside a document', () => {
	test('comment on part of a document image, send from the viewer, and find it again in the document (1440)', async ({ page, request }, testInfo) => {
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
		// FR13.12: the viewer shows the document's pending-send status and Send, short in the bar and read out in full.
		await expect(viewerSend(page)).toHaveText(/^\s*Send 1\s*$/);
		await expect(viewerSend(page)).toHaveAttribute('aria-label', /^Send 1 comment to /);
		await page.keyboard.press('Escape');
		await expectBarFits(page);
		await shoot(page, testInfo, 'doc-1440-03-viewer-pending-send', true);

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
		await card.click();
		await expect(overlay(page)).toBeVisible();
		await expect(tag(page, 1)).toHaveAttribute('aria-pressed', 'true');
		await page.keyboard.press('Escape');

		// Send in the viewer closes the document's batch: nothing is left to send in either place.
		const sent = page.waitForRequest((req) => req.method() === 'POST' && req.url().includes(`/api/doc-feedback/send`));
		await viewerSend(page).click();
		const body = (await sent).postDataJSON() as { artifact_id?: string } | null;
		if (body?.artifact_id) expect(body.artifact_id).toBe(seeded.artifactId);
		// It says Sent for a moment, then goes.
		await expect(bar(page).locator('.sent')).toHaveText(/^\s*Sent\s*$/);
		await saveEvidenceScreenshot(page, testInfo, 'doc-1440-07-viewer-sent-confirmed-dark', { fullPage: false });
		await expect(bar(page).locator('.sending-now')).toHaveCount(0);
		await shoot(page, testInfo, 'doc-1440-07-viewer-sent', true);
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(docComments(page)).not.toHaveAttribute('aria-label', /not sent yet/);
		await shoot(page, testInfo, 'doc-1440-08-document-sent', true);
	});

	test('on a phone the viewer bar keeps Back, Fit, Comment and the count, and adds Send only while pending (393)', async ({ page, request }, testInfo) => {
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
		await expect(viewerSend(page)).toHaveText(/^\s*Send 1\s*$/);
		await expect(viewerSend(page)).toHaveAttribute('aria-label', /^\s*Send 1 comment to /);
		await page.keyboard.press('Escape');
		// Every bar control keeps a 44 px target and the bar stays one row inside the screen.
		for (const control of await bar(page).locator('button').all()) {
			if (!(await control.isVisible())) continue;
			expect((await control.boundingBox())!.height, await control.innerText()).toBeGreaterThanOrEqual(44);
		}
		await expectBarFits(page);
		await shoot(page, testInfo, 'doc-393-03-viewer-pending-send', true);

		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(docComments(page)).toHaveAttribute('aria-label', unsentName(1));
		await expectMarkersBesideImages(page, testInfo, 'doc-393-04-document-marker-pending');
		await docComments(page).click();
		await expect(page.locator('.drawer-panel[role="dialog"], .canvas-comment-drawer').getByText('Tap target too small')).toBeVisible();
		await expect(drawerThumb(page)).toBeVisible();
		await expect(drawer(page).locator('[data-drawer-unsent]')).toContainText('1 message not sent yet');
		await shoot(page, testInfo, 'doc-393-05-drawer-image-group', true);
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
	});

	for (const width of [1440, 393]) test(`the viewer bar shows the sending countdown, and a batch not delivered with Retry (${width})`, async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(180_000);
		await page.setViewportSize({ width, height: width > 768 ? 900 : 852 });
		const seeded = await seedDocument(page, request, `doc-image-regions-states-${width}`, null);
		// The server sets these on its own clock or on a failed delivery; the frames rewrite its answers to show them.
		const inject: { grace?: string; undelivered?: unknown[] } = {};
		await page.route('**/api/artifacts/*/comments*', async (route) => {
			if (route.request().method() !== 'GET') return route.fallback();
			const response = await route.fetch();
			const json = await response.json();
			if (json.feedback && inject.grace) json.feedback.grace_closes_at = inject.grace;
			if (json.feedback && inject.undelivered) json.feedback.undelivered_batches = inject.undelivered;
			await route.fulfill({ response, json });
		});
		await openDocument(page, seeded, width);
		inject.grace = new Date(Date.now() + 3 * 60_000).toISOString();
		await openImage(page);
		await commentOnRegion(page, 'The header crowds the logo');
		await page.keyboard.press('Escape');
		await expect(viewerSend(page)).toHaveText(/^\s*Send now\s*$/);
		await expect(viewerSend(page)).toHaveAttribute('aria-label', /^Sending to .+ in \d+ min\. Send now$/);
		await expectBarFits(page);
		await shoot(page, testInfo, `doc-${width}-09-viewer-sending-grace`, true);

		inject.grace = undefined;
		inject.undelivered = [{ id: 'batch-1', item_count: 1, undelivered_reason: 'owner_unavailable', attempts: 1 }];
		await page.route('**/api/doc-feedback/send', (route) => route.fulfill({ json: { woken: [], undelivered_batches: inject.undelivered } }));
		await viewerSend(page).click();
		const retry = bar(page).getByRole('button', { name: /Retry/ });
		await expect(retry).toBeVisible();
		await expect(bar(page).locator('.sent')).toHaveCount(0);
		await expectBarFits(page);
		if (width <= 768) expect((await retry.boundingBox())!.height, 'Retry keeps a 44 px target').toBeGreaterThanOrEqual(44);
		await shoot(page, testInfo, `doc-${width}-10-viewer-undelivered-retry`, true);
	});

	for (const [label, width, reduced] of [['1440', 1440, false], ['393', 393, false], ['1440-reduced-motion', 1440, true]] as const) {
		test(`Send grows into the viewer bar, says Sent, then leaves with nothing jumping (recorded, ${label})`, async ({ browser, request }, testInfo) => {
			mockOnly(testInfo);
			test.setTimeout(180_000);
			const size = { width, height: width > 768 ? 900 : 852 };
			const context = await browser.newContext({
				baseURL: testInfo.project.use.baseURL, viewport: size, colorScheme: 'dark',
				reducedMotion: reduced ? 'reduce' : 'no-preference', recordVideo: { dir: testInfo.outputPath('video'), size },
			});
			const page = await context.newPage();
			// The page's animations run ten times slower, from before each trigger to the end, so the recording and the
			// per-frame samples resolve the motion; each motion's own keyframes and duration are read at real speed.
			const cdp = await context.newCDPSession(page);
			const slow = async () => { await cdp.send('Animation.enable'); await cdp.send('Animation.setPlaybackRate', { playbackRate: 0.1 }); };
			try {
				const seeded = await seedDocument(page, request, `doc-send-motion-${label}`, null);
				await openDocument(page, seeded, width);
				await openImage(page);
				await expect(bar(page).locator('.sending-now')).toHaveCount(0);
				await slow();
				await startSampling(page);
				await commentOnRegion(page, 'The header crowds the logo');
				await expect(viewerSend(page)).toBeVisible();
				await page.waitForTimeout(3000);
				const arrival = await stopSampling(page);
				await putSheetAway(page);

				// Send turns into Sent where it stands; the leave is measured from Sent on.
				await viewerSend(page).click();
				await expect(bar(page).locator('.sent')).toBeVisible();
				await startSampling(page);
				await expect(bar(page).locator('.sending-now')).toHaveCount(0, { timeout: 15_000 });
				await page.waitForTimeout(300);
				const departure = await stopSampling(page);
				fs.writeFileSync(testInfo.outputPath('samples.json'), JSON.stringify({ playbackRate: 0.1, arrival, departure }));

				if (reduced) {
					// Reduced motion keeps only the fade: no space opens or closes, and Send passes through partial opacity.
					const fading = (seen: Sample[]) => seen.some(([, , , opacity]) => opacity !== null && opacity > 0.05 && opacity < 0.95);
					expect(arrival.motions, 'arrival opens no space').toEqual([]);
					expect(departure.motions, 'departure closes no space').toEqual([]);
					expect(fading(arrival.seen), `arrival fades ${JSON.stringify(arrival.seen)}`).toBe(true);
					expect(fading(departure.seen), `departure fades ${JSON.stringify(departure.seen)}`).toBe(true);
				} else {
					// At real speed: Send grows in from nothing as it fades in; Sent fades, then its space closes.
					expect(arrival.motions).toHaveLength(1);
					expect(arrival.motions[0]).toMatchObject({ from: { maxWidth: '0px', opacity: 0 }, to: { opacity: 1 }, duration: 220 });
					expect(arrival.motions[0].to.maxWidth).not.toBe('0px');
					expect(departure.motions).toHaveLength(1);
					expect(departure.motions[0]).toMatchObject({ from: { opacity: 1 }, to: { maxWidth: '0px', opacity: 0 }, duration: 340 });
					expect(departure.motions[0].from.maxWidth).not.toBe('0px');
					expectGlide(arrival.seen, 'arrival');
					expectGlide(departure.seen, 'departure');
				}
			} finally {
				await cdp.send('Animation.setPlaybackRate', { playbackRate: 1 }).catch(() => {});
				const video = page.video();
				await context.close();
				await video?.saveAs(testInfo.outputPath(`send-motion-${label}.webm`));
			}
		});
	}
});
