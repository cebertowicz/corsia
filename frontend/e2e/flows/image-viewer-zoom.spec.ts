import fs from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import {
	createSession,
	loginByUrl,
	saveEvidenceScreenshot,
	TEST_TOKEN,
} from '../support/corsia';
import { syntheticPng } from '../support/syntheticImages';

const NATURAL = { width: 1600, height: 1000 };
const filler = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => `Paragraph ${from + i}: the walkthrough continues with enough words to wrap across a few lines of the document column at any width.\n`).join('\n');
const DOC = [
	'# Design walkthrough', '', filler(1, 14), '![Mock screen](shots/screen.png)', '', filler(14, 30), '![Gone](shots/gone.png)', '', filler(30, 40),
].join('\n');

interface Seeded { sessionId: string; sessionTitle: string; dir: string; docPath: string; artifactId: string }

/** A document with a large image in the middle, a missing one after it, and filler to scroll through. */
async function seedDocument(page: Page, request: APIRequestContext, label: string): Promise<Seeded> {
	const stamp = Date.now();
	const sessionTitle = `${label}-${stamp}`;
	const sessionId = await createSession(request, sessionTitle);
	const tok = encodeURIComponent(TEST_TOKEN);
	const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
	const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
	const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
	expect(workspaceRoot).toEqual(expect.any(String));
	const dir = path.join(workspaceRoot, `${label}-${stamp}`);
	const docPath = path.join(dir, 'walkthrough.md');
	fs.mkdirSync(path.join(dir, 'shots'), { recursive: true });
	fs.writeFileSync(path.join(dir, 'shots', 'screen.png'), syntheticPng(NATURAL.width, NATURAL.height));
	fs.writeFileSync(docPath, DOC);
	const registered = await request.post(`/api/files/register?token=${tok}`, { data: { path: docPath, thread_id: sessionId } });
	expect(registered.ok(), `register failed: ${registered.status()}`).toBeTruthy();
	const artifact = (await registered.json()).artifact;
	await page.addInitScript(
		({ threadId, artifactId, tabPath }) => {
			localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
				threads: {
					[threadId]: {
						tabs: [{ instanceId: 'tab-doc', artifactId, path: tabPath, title: 'walkthrough.md', loadedRevision: null, latestRevision: null, status: 'loading' }],
						activeTabId: 'tab-doc', visible: true, activeSurface: 'artifact',
					},
				},
				splitWidth: null,
			}));
		},
		{ threadId: sessionId, artifactId: artifact.id, tabPath: docPath },
	);
	return { sessionId, sessionTitle, dir, docPath, artifactId: artifact.id };
}

async function openThread(page: Page, title: string, width: number) {
	await loginByUrl(page);
	if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title).first().click();
}

const leafWidth = async (page: Page) => (await page.locator('.leaf').first().boundingBox())!.width;
const docScroll = (page: Page) => page.locator('.cm-scroller').first().evaluate((el) => el.scrollTop);
/** Where the document's first image sits in the scroller; it holds still across a pane reflow, where the raw scroll offset need not. */
const imageOffset = (page: Page) => page.evaluate(() => {
	const image = document.querySelector('.cm-md-image img')!;
	return image.getBoundingClientRect().top - document.querySelector('.cm-scroller')!.getBoundingClientRect().top;
});
const frame = (page: Page) => page.locator('.image-overlay .frame');
const frameWidth = async (page: Page) => (await frame(page).boundingBox())!.width;
const level = (page: Page) => page.locator('.image-overlay .level');
/**
 * The frame's width and the width Fit gives it inside the viewport's 24px margins, read in one layout. The pane is still
 * widening when the viewer opens, so the two read in separate calls belong to different frames of that animation.
 */
const fitWidths = (page: Page) => page.evaluate((natural) => {
	const viewport = document.querySelector('.image-overlay .viewport')!;
	const drawn = document.querySelector('.image-overlay .frame')!.getBoundingClientRect().width;
	const scale = Math.min(1, (viewport.clientWidth - 48) / natural.width, (viewport.clientHeight - 48) / natural.height);
	return { drawn: Math.round(drawn), fit: Math.round(natural.width * scale) };
}, NATURAL);
const atFit = async (page: Page) => {
	const { drawn, fit } = await fitWidths(page);
	return Math.abs(drawn - fit) <= 1;
};

/**
 * The editor draws only what is near the viewport, so a thing far down the document appears once the text has scrolled to it.
 * It scrolls with the wheel: after Back, the document holds the image where it was until the person touches it (documentImagePlace.ts,
 * USER_INPUT), and a script's scrollTop write is not that.
 */
async function reveal(page: Page, selector: string) {
	const found = page.locator(selector).first();
	await expect.poll(async () => {
		if (await found.count()) return true;
		await page.locator('.cm-scroller').first().hover();
		await page.mouse.wheel(0, 250);
		return false;
	}, { intervals: [100] }).toBe(true);
	return found;
}
const revealImage = (page: Page) => reveal(page, '.cm-md-image img');

/** Brings the document's image into the middle of the visible text so Back has a place to return to. */
async function scrollImageIntoUpperView(page: Page) {
	await page.locator('.cm-md-image').first().evaluate((el) => {
		const scroller = el.closest('.cm-scroller')!;
		scroller.scrollTop += el.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 90;
	});
	await page.waitForTimeout(150);
}

test.describe('@flow:image-viewer-zoom @surface:documents the canvas image viewer zooms', () => {
	test.use({ hasTouch: true });

	test('opens a document image at Fit, zooms and pans it, and returns to the document where it was (1440)', async ({ page, request, context }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		await page.setViewportSize({ width: 1440, height: 900 });
		const other = await createSession(request, `viewer-other-${Date.now()}`);
		const seeded = await seedDocument(page, request, 'viewer-zoom-1440');
		try {
			await openThread(page, seeded.sessionTitle, 1440);
			const image = await revealImage(page);
			await expect(image).toBeVisible();
			await expect.poll(() => image.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(NATURAL.width);
			await scrollImageIntoUpperView(page);

			const pagesBefore = context.pages().length;
			const scrollBefore = await docScroll(page);
			const offsetBefore = await imageOffset(page);
			const widthBefore = await leafWidth(page);
			expect(scrollBefore).toBeGreaterThan(200);

			// A click opens the viewer in the same tab at Fit; no tab or window is opened.
			await image.click();
			const overlay = page.locator('.image-overlay');
			await expect(overlay.locator('.frame img')).toBeVisible();
			expect(context.pages().length).toBe(pagesBefore);
			await expect(page.locator('.tab')).toHaveCount(1);
			await expect(overlay.getByRole('button', { name: 'Back to walkthrough.md' })).toBeVisible();
			await expect(overlay.getByRole('button', { name: 'Fit' })).toHaveAttribute('aria-pressed', 'true');
			// The workspace pane gives the leaf room, unless it was already that wide.
			await expect.poll(() => leafWidth(page)).toBeGreaterThanOrEqual(Math.min(600, Math.max(widthBefore, 600)) - 1);
			await expect.poll(() => atFit(page)).toBe(true);
			const fitted = await frame(page).boundingBox();
			expect(fitted!.width / fitted!.height).toBeCloseTo(NATURAL.width / NATURAL.height, 1);
			// The document is withdrawn: nothing of it takes a click or a key.
			await expect(page.locator('.doc-host')).toHaveAttribute('inert', '');
			await expect(page.locator('.sel-comment')).toHaveCount(0);
			await page.emulateMedia({ colorScheme: 'dark' });
			await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
			await saveEvidenceScreenshot(page, testInfo, 'viewer-1440-fit-dark', { fullPage: false });
			await page.emulateMedia({ colorScheme: 'light' });
			await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
			await saveEvidenceScreenshot(page, testInfo, 'viewer-1440-fit-light', { fullPage: false });
			await page.emulateMedia({ colorScheme: 'dark' });

			// 100% shows the image at its own pixels, and the bar says so.
			await overlay.getByRole('button', { name: '100%' }).click();
			await expect.poll(() => frameWidth(page)).toBeCloseTo(NATURAL.width, 0);
			await expect(level(page)).toHaveText('100%');
			await expect(overlay.getByRole('button', { name: '100%' })).toHaveAttribute('aria-pressed', 'true');
			await saveEvidenceScreenshot(page, testInfo, 'viewer-1440-100-dark', { fullPage: false });
			await overlay.getByRole('button', { name: 'Zoom in' }).click();
			await expect(level(page)).toHaveText('125%');
			await overlay.getByRole('button', { name: 'Zoom out' }).click();
			await expect(level(page)).toHaveText('100%');

			// Pan by Space-drag (a bare drag draws a box), and by the scrollbars the region carries; the image is larger than the viewport.
			const region = page.locator('.image-overlay .viewport');
			const scrollable = await region.evaluate((el) => ({ x: el.scrollWidth > el.clientWidth, y: el.scrollHeight > el.clientHeight }));
			expect(scrollable).toEqual({ x: true, y: true });
			const viewport = (await region.boundingBox())!;
			const spot = { x: viewport.x + viewport.width / 2, y: viewport.y + viewport.height / 2 };
			const panFrom = await region.evaluate((el) => ({ left: el.scrollLeft, top: el.scrollTop }));
			await page.keyboard.down('Space');
			await page.mouse.move(spot.x, spot.y);
			await page.mouse.down();
			await page.mouse.move(spot.x - 120, spot.y - 80, { steps: 6 });
			await page.mouse.up();
			await page.keyboard.up('Space');
			// Space only pans: the zoom button that still has focus is not pressed by it.
			await expect(level(page)).toHaveText('100%');
			const panned = await region.evaluate((el) => ({ left: el.scrollLeft, top: el.scrollTop }));
			expect(panned.left - panFrom.left).toBeGreaterThan(100);
			expect(panned.top - panFrom.top).toBeGreaterThan(60);
			await page.mouse.wheel(0, 60);
			await expect.poll(() => region.evaluate((el) => el.scrollTop)).toBeGreaterThan(panned.top + 20);

			// Cmd-scroll and a trackpad pinch (ctrl-wheel) zoom around the pointer: the image point under it stays put.
			const underPointer = async () => {
				const box = (await frame(page).boundingBox())!;
				return { x: (spot.x - box.x) / box.width, y: (spot.y - box.y) / box.height };
			};
			const before = await underPointer();
			const scaleBefore = await frameWidth(page);
			await page.mouse.move(spot.x, spot.y);
			await page.keyboard.down('Meta');
			await page.mouse.wheel(0, -90);
			await page.keyboard.up('Meta');
			await expect.poll(() => frameWidth(page)).toBeGreaterThan(scaleBefore + 20);
			const after = await underPointer();
			expect(Math.abs(after.x - before.x)).toBeLessThan(0.01);
			expect(Math.abs(after.y - before.y)).toBeLessThan(0.01);
			const scaleMid = await frameWidth(page);
			await page.keyboard.down('Control');
			await page.mouse.wheel(0, 90);
			await page.keyboard.up('Control');
			await expect.poll(() => frameWidth(page)).toBeLessThan(scaleMid - 20);
			await overlay.getByRole('button', { name: 'Fit' }).click();
			await expect.poll(() => atFit(page)).toBe(true);

			// Focus ring on the bar and the reduced-motion state.
			await page.keyboard.press('Tab');
			await saveEvidenceScreenshot(page, testInfo, 'viewer-1440-focus-ring', { fullPage: false });
			await page.emulateMedia({ reducedMotion: 'reduce' });
			await overlay.getByRole('button', { name: '100%' }).click();
			await expect(level(page)).toHaveText('100%');
			await saveEvidenceScreenshot(page, testInfo, 'viewer-1440-reduced-motion', { fullPage: false });
			await page.emulateMedia({ reducedMotion: 'no-preference' });

			// Back returns to the document at the same place, with the image in view and the pane as it was.
			await overlay.getByRole('button', { name: 'Back to walkthrough.md' }).click();
			await expect(overlay).toHaveCount(0);
			await expect(page.locator('.doc-host')).not.toHaveAttribute('inert', '');
			await expect.poll(() => leafWidth(page)).toBeCloseTo(widthBefore, 0);
			await expect.poll(async () => Math.abs((await imageOffset(page)) - offsetBefore)).toBeLessThan(6);
			await expect(page.locator('.cm-md-image img').first()).toBeInViewport();
			await saveEvidenceScreenshot(page, testInfo, 'viewer-1440-back-in-doc', { fullPage: false });

			// Escape is the other way back; a missing image keeps its note and never opens the viewer.
			await image.click();
			await expect(overlay.locator('.frame img')).toBeVisible();
			await page.keyboard.press('Escape');
			await expect(overlay).toHaveCount(0);
			await expect.poll(() => leafWidth(page)).toBeCloseTo(widthBefore, 0);
			await expect.poll(async () => Math.abs((await imageOffset(page)) - offsetBefore)).toBeLessThan(6);
			const note = await reveal(page, '.cm-md-image-note');
			await note.scrollIntoViewIfNeeded();
			await expect(note).toHaveText('Gone · Image not found');
			await note.click();
			await expect(overlay).toHaveCount(0);
			await expect(page.locator('.cm-md-image-selected')).toHaveCount(1);
			await saveEvidenceScreenshot(page, testInfo, 'viewer-1440-missing-image-in-doc', { fullPage: false });
			// Back to the working image: the editor has dropped it from the page while the missing one was shown.
			await page.locator('.cm-scroller').first().evaluate((el) => { el.scrollTop = 0; });
			await revealImage(page);
			await scrollImageIntoUpperView(page);

			// Enter on a selected image opens it, from the keyboard alone.
			await page.getByText('Paragraph 14:').first().click({ position: { x: 2, y: 2 } });
			await page.keyboard.press('Home');
			await page.keyboard.press('Shift+ArrowUp');
			await page.keyboard.press('Shift+ArrowUp');
			await expect(page.locator('.cm-md-image-selected')).toHaveCount(1);
			await page.keyboard.press('Enter');
			await expect(overlay.locator('.frame img')).toBeVisible();

			// Switching threads hides the global overlay but preserves this tab's image camera; Back still restores the document pane.
			await page.getByText(/viewer-other-/).first().click();
			await expect(overlay).toHaveCount(0);
			await page.getByText(seeded.sessionTitle).first().click();
			await expect(overlay.locator('.frame img')).toBeVisible();
			await overlay.getByRole('button', { name: 'Back to walkthrough.md' }).click();
			await expect(overlay).toHaveCount(0);
			await expect(await revealImage(page)).toBeVisible();
			await expect.poll(() => leafWidth(page)).toBeCloseTo(widthBefore, 0);
		} finally {
			fs.rmSync(seeded.dir, { recursive: true, force: true });
			void other;
		}
	});

	test('opens a standalone image artifact in the same viewer, with no Back and no layout change, and shows a broken image working (1440)', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		await page.setViewportSize({ width: 1440, height: 900 });
		const stamp = Date.now();
		const sessionTitle = `viewer-artifact-${stamp}`;
		const sessionId = await createSession(request, sessionTitle);
		const tok = encodeURIComponent(TEST_TOKEN);
		const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
		const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
		const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
		const good = path.join(workspaceRoot, `viewer-good-${stamp}.png`);
		const broken = path.join(workspaceRoot, `viewer-broken-${stamp}.png`);
		fs.writeFileSync(good, syntheticPng(NATURAL.width, NATURAL.height));
		fs.writeFileSync(broken, Buffer.from('not really a png'));
		try {
			const tabs: Array<Record<string, unknown>> = [];
			for (const [index, file] of [good, broken].entries()) {
				const registered = await request.post(`/api/files/register?token=${tok}`, { data: { path: file, thread_id: sessionId } });
				expect(registered.ok(), `register failed: ${registered.status()}`).toBeTruthy();
				const artifact = (await registered.json()).artifact;
				tabs.push({ instanceId: `tab-${index}`, artifactId: artifact.id, path: file, title: path.basename(file), loadedRevision: null, latestRevision: null, status: 'loading' });
			}
			await page.addInitScript(
				({ threadId, tabList }) => {
					localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
						threads: { [threadId]: { tabs: tabList, activeTabId: 'tab-0', visible: true, activeSurface: 'artifact' } },
						splitWidth: null,
					}));
				},
				{ threadId: sessionId, tabList: tabs },
			);
			await openThread(page, sessionTitle, 1440);
			const frameImg = page.locator('.image-viewer .frame img');
			await expect(frameImg).toBeVisible();
			await expect.poll(() => frameImg.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(NATURAL.width);
			await expect(page.locator('.image-viewer button.back')).toHaveCount(0);
			const widthBefore = await leafWidth(page);
			const viewer = page.locator('.image-viewer');
			await expect(viewer.getByRole('button', { name: 'Fit' })).toHaveAttribute('aria-pressed', 'true');
			await viewer.getByRole('button', { name: '100%' }).click();
			await expect(viewer.locator('.level')).toHaveText('100%');
			await saveEvidenceScreenshot(page, testInfo, 'artifact-1440-100-dark', { fullPage: false });
			await viewer.getByRole('button', { name: 'Fit' }).click();
			await page.emulateMedia({ colorScheme: 'light' });
			await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
			await saveEvidenceScreenshot(page, testInfo, 'artifact-1440-fit-light', { fullPage: false });
			await page.emulateMedia({ colorScheme: 'dark' });
			expect(await leafWidth(page)).toBeCloseTo(widthBefore, 0);

			// A file that is not a decodable image: a plain failure, with a way to try again, and the leaf still works.
			await page.getByText(path.basename(broken)).first().click();
			await expect(page.getByText("Couldn't load this image.")).toBeVisible();
			await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
			await saveEvidenceScreenshot(page, testInfo, 'artifact-1440-broken', { fullPage: false });
			await page.getByRole('button', { name: 'Try again' }).click();
			await expect(page.getByText("Couldn't load this image.")).toBeVisible();
		} finally {
			fs.rmSync(good, { force: true });
			fs.rmSync(broken, { force: true });
		}
	});

	test('phone: Back, comment controls and Fit, one finger pans, two pinch, and Back returns to the document (393)', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		await page.setViewportSize({ width: 393, height: 852 });
		const seeded = await seedDocument(page, request, 'viewer-zoom-393');
		try {
			await openThread(page, seeded.sessionTitle, 393);
			const image = await revealImage(page);
			await expect(image).toBeVisible();
			await expect.poll(() => image.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(NATURAL.width);
			await scrollImageIntoUpperView(page);
			const scrollBefore = await docScroll(page);
			expect(scrollBefore).toBeGreaterThan(200);

			await image.tap();
			const overlay = page.locator('.image-overlay');
			await expect(overlay.locator('.frame img')).toBeVisible();
			const labels = await overlay.locator('.bar button').evaluateAll((buttons) => buttons.map((b) => b.getAttribute('aria-label') ?? b.textContent?.trim()));
			expect(labels).toEqual(['Back to walkthrough.md', 'Comment', '0 open threads', 'Fit']);
			await expect(overlay.locator('.back-text')).toHaveText('Back');
			await expect(overlay.getByRole('button', { name: 'Fit' })).toHaveAttribute('aria-pressed', 'true');
			expect((await page.locator('.image-overlay .bar button').first().boundingBox())!.height).toBeGreaterThanOrEqual(44);
			await page.emulateMedia({ colorScheme: 'dark' });
			await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
			await saveEvidenceScreenshot(page, testInfo, 'viewer-393-fit-dark', { fullPage: false });
			await page.emulateMedia({ colorScheme: 'light' });
			await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
			await saveEvidenceScreenshot(page, testInfo, 'viewer-393-fit-light', { fullPage: false });
			await page.emulateMedia({ colorScheme: 'dark' });

			const cdp = await page.context().newCDPSession(page);
			const touch = (type: 'touchStart' | 'touchMove' | 'touchEnd', points: Array<{ x: number; y: number }>) =>
				cdp.send('Input.dispatchTouchEvent', { type, touchPoints: points.map((p, id) => ({ x: p.x, y: p.y, id })) });
			const box = (await page.locator('.image-overlay .viewport').boundingBox())!;
			const centre = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
			const fitWidth = await frameWidth(page);

			// Two fingers apart zoom in, around where they are.
			await touch('touchStart', [{ x: centre.x - 30, y: centre.y }, { x: centre.x + 30, y: centre.y }]);
			for (let step = 1; step <= 8; step++) {
				await touch('touchMove', [{ x: centre.x - 30 - step * 12, y: centre.y }, { x: centre.x + 30 + step * 12, y: centre.y }]);
			}
			await touch('touchEnd', []);
			await expect.poll(() => frameWidth(page)).toBeGreaterThan(fitWidth * 1.8);
			await expect(overlay.getByRole('button', { name: 'Fit' })).toHaveAttribute('aria-pressed', 'false');
			await saveEvidenceScreenshot(page, testInfo, 'viewer-393-pinched-dark', { fullPage: false });

			// One finger pans the zoomed image.
			const region = page.locator('.image-overlay .viewport');
			const from = await region.evaluate((el) => ({ left: el.scrollLeft, top: el.scrollTop }));
			await touch('touchStart', [{ x: centre.x + 80, y: centre.y + 80 }]);
			for (let step = 1; step <= 8; step++) await touch('touchMove', [{ x: centre.x + 80 - step * 12, y: centre.y + 80 - step * 10 }]);
			// A finger that stops before it lifts. Lifted at the speed of these synthetic moves, Chromium on Linux
			// starts a fling and drops the next tap's click (it only stops the fling), whatever the page does.
			await page.waitForTimeout(150);
			await touch('touchEnd', []);
			await expect.poll(() => region.evaluate((el) => el.scrollLeft)).toBeGreaterThan(from.left + 40);
			expect(await region.evaluate((el) => el.scrollTop)).toBeGreaterThan(from.top + 30);

			await overlay.getByRole('button', { name: 'Fit' }).click();
			await expect.poll(() => frameWidth(page)).toBeCloseTo(fitWidth, 0);

			await overlay.getByRole('button', { name: 'Back to walkthrough.md' }).tap();
			await expect(overlay).toHaveCount(0);
			await expect.poll(async () => Math.abs((await docScroll(page)) - scrollBefore)).toBeLessThan(6);
			await expect(page.locator('.cm-md-image img').first()).toBeInViewport();
			await saveEvidenceScreenshot(page, testInfo, 'viewer-393-back-in-doc', { fullPage: false });
		} finally {
			fs.rmSync(seeded.dir, { recursive: true, force: true });
		}
	});
});
