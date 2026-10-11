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
const tag = (page: Page, number: number) => overlay(page).locator(`[data-region-tag="${number}"]:not([inert])`);
const viewerSend = (page: Page) => bar(page).getByRole('button', { name: /^Send/ });
const docSend = (page: Page) => page.locator('.document-status-row').getByRole('button', { name: /^Send/ });
const docComments = (page: Page) => page.locator('.document-status-row .comment-status');
const drawerThumb = (page: Page) => page.locator('.canvas-comment-drawer, .drawer-panel[role="dialog"]').locator('[data-region-thumb]').first();
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
/** Draws a box over part of the image and posts a comment on it. */
async function commentOnRegion(page: Page, text: string, at = { x: 0.2, y: 0.25 }) {
	await bar(page).locator('[data-image-comment]').click();
	const box = (await surface(page).boundingBox())!;
	const from = { x: box.x + box.width * at.x, y: box.y + box.height * at.y };
	const to = { x: box.x + box.width * (at.x + 0.25), y: box.y + box.height * (at.y + 0.25) };
	await drag(page, from, to);
	const composer = page.locator('textarea[placeholder="Comment…"]');
	await expect(composer).toBeVisible();
	await composer.fill(text);
	await page.keyboard.press('ControlOrMeta+Enter');
	await expect(composer).toHaveCount(0);
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
/**
 * The margin marker for a comment on the image sits beside the image, not the text above it, in either theme; each
 * theme's frame is taken once it has settled.
 */
async function expectMarkerBesideImage(page: Page, testInfo: TestInfo, name: string) {
	// A theme switch redraws the image; leaving the viewer can reflow the canvas. Measure once nothing moves.
	for (const scheme of ['light', 'dark'] as const) {
		await page.emulateMedia({ colorScheme: scheme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', scheme);
		let last = '';
		await expect.poll(async () => {
			const now = JSON.stringify([await docImage(page).boundingBox(), await marker(page).boundingBox()]);
			const same = now === last;
			last = now;
			return same;
		}, { intervals: [250] }).toBe(true);
		const image = (await docImage(page).boundingBox())!;
		const mark = (await marker(page).boundingBox())!;
		const middle = mark.y + mark.height / 2;
		expect(middle, `${scheme}: marker at ${middle} beside the image (${image.y}–${image.y + image.height})`).toBeGreaterThanOrEqual(image.y);
		expect(middle, `${scheme}: marker beside the image`).toBeLessThanOrEqual(image.y + image.height);
		await saveEvidenceScreenshot(page, testInfo, `${name}-${scheme}`, { fullPage: false });
	}
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
		await expect(docSend(page)).toHaveText(/^\s*Send 1 comment to /);
		await expect(marker(page)).toBeVisible();
		await expectMarkerBesideImage(page, testInfo, 'doc-1440-04-document-marker-pending');

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
		await expect(viewerSend(page)).toHaveCount(0);
		await shoot(page, testInfo, 'doc-1440-07-viewer-sent', true);
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(docSend(page)).toHaveCount(0);
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
		await expect(docSend(page)).toHaveText(/^\s*Send 1 comment to /);
		await expectMarkerBesideImage(page, testInfo, 'doc-393-04-document-marker-pending');
		await docComments(page).click();
		await expect(page.locator('.drawer-panel[role="dialog"], .canvas-comment-drawer').getByText('Tap target too small')).toBeVisible();
		await expect(drawerThumb(page)).toBeVisible();
		await shoot(page, testInfo, 'doc-393-05-drawer-image-group', true);
	});

	test('many comments on two images: tags in the viewer, a marker per image, a drawer group per image (1440)', async ({ page, request }, testInfo) => {
		mockOnly(testInfo);
		test.setTimeout(240_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		const seeded = await seedDocument(page, request, 'doc-image-regions-many', null, DOC_TWO);
		await openDocument(page, seeded, 1440);
		const notes = [
			['The header crowds the logo', { x: 0.05, y: 0.05 }], ['Search needs a label', { x: 0.4, y: 0.1 }],
			['This card is too tall', { x: 0.1, y: 0.5 }], ['Footer links are low contrast', { x: 0.55, y: 0.6 }],
		] as const;
		await openImage(page, 0);
		for (const [text, at] of notes) await commentOnRegion(page, text, at);
		await page.keyboard.press('Escape');
		await expect(tag(page, 4)).toBeVisible();
		await shoot(page, testInfo, 'doc-1440-many-01-viewer-four-tags', true);
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);

		await openImage(page, 1);
		await commentOnRegion(page, 'Toggle labels are unclear', { x: 0.1, y: 0.2 });
		await commentOnRegion(page, 'Save should be primary', { x: 0.6, y: 0.65 });
		await page.keyboard.press('Escape');
		await bar(page).getByRole('button', { name: 'Back to walkthrough.md' }).click();
		await expect(overlay(page)).toHaveCount(0);
		// A marker beside each image, and the document's row counting all six.
		await expect(page.locator('.cm-comment-marker')).toHaveCount(2);
		await expect(docSend(page)).toHaveText(/^\s*Send 6 comments to /);
		await shoot(page, testInfo, 'doc-1440-many-02-document-two-markers', true);

		// The drawer: one group per image, each under one thumbnail, comments in number order; scrolled to the second.
		await docComments(page).click();
		const thumbs = page.locator('.canvas-comment-drawer [data-region-thumb]');
		await expect(thumbs).toHaveCount(2);
		await shoot(page, testInfo, 'doc-1440-many-03-drawer-two-groups', true);
		await thumbs.nth(1).scrollIntoViewIfNeeded();
		await expect(page.locator('.canvas-comment-drawer').getByText('Save should be primary')).toBeVisible();
		await shoot(page, testInfo, 'doc-1440-many-04-drawer-scrolled', true);
	});
});
