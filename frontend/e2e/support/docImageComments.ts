import fs from 'node:fs';
import path from 'node:path';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from './corsia';
import { drag } from './imageRegionComments';
import { syntheticPng } from './syntheticImages';
import { turnOffDocumentComments } from './heldComments';

// A document with images in it, open in its thread's canvas, and the image viewer laid over it: the fixtures every
// spec that comments on a document's image shares.

export const DOC_IMAGE_NATURAL = { width: 1600, height: 1000 };
const filler = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => `Paragraph ${from + i}: the walkthrough continues with enough words to wrap across a few lines of the document column.\n`).join('\n');
export const DOC = ['# Design walkthrough', '', filler(1, 4), '![Mock screen](shots/screen.png)', '', filler(4, 12)].join('\n');
/** Two images, for many comments on each and a drawer with more than one image group. */
export const DOC_TWO = ['# Design walkthrough', '', filler(1, 2), '![Mock screen](shots/screen.png)', '', filler(2, 4), '![Settings screen](shots/settings.png)', '', filler(4, 12)].join('\n');
const query = `?token=${encodeURIComponent(TEST_TOKEN)}`;

export interface SeededImageDocument { sessionId: string; sessionTitle: string; artifactId: string; imagePath: string }

/** A document with one image near its top, open in its thread's canvas. */
export async function seedImageDocument(
	page: Page, request: APIRequestContext, label: string, splitWidth: number | null, doc = DOC,
	/** Seed the canvas tabs on the first load only, so a reload shows what the app itself restores. */
	seedOnce = false,
): Promise<SeededImageDocument> {
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
	for (const name of ['screen.png', 'settings.png']) fs.writeFileSync(path.join(dir, 'shots', name), syntheticPng(DOC_IMAGE_NATURAL.width, DOC_IMAGE_NATURAL.height));
	fs.writeFileSync(docPath, doc);
	const registered = await request.post(`/api/files/register${query}`, { data: { path: docPath, thread_id: sessionId } });
	expect(registered.ok(), `register failed: ${registered.status()}`).toBeTruthy();
	const artifactId = (await registered.json()).artifact.id as string;
	await page.addInitScript(
		({ threadId, id, tabPath, width, once }) => {
			if (once) {
				if (sessionStorage.getItem('e2e:workspace-seeded')) return;
				sessionStorage.setItem('e2e:workspace-seeded', '1');
			}
			localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
				threads: { [threadId]: {
					tabs: [{ instanceId: 'tab-doc', artifactId: id, path: tabPath, title: 'walkthrough.md', loadedRevision: null, latestRevision: null, status: 'loading' }],
					activeTabId: 'tab-doc', visible: true, activeSurface: 'artifact',
				} },
				splitWidth: width,
			}));
		},
		{ threadId: sessionId, id: artifactId, tabPath: docPath, width: splitWidth, once: seedOnce },
	);
	return { sessionId, sessionTitle, artifactId, imagePath: path.join(dir, 'shots', 'screen.png') };
}

export const overlay = (page: Page) => page.locator('.image-overlay');
export const bar = (page: Page) => overlay(page).locator('.bar');
export const docImage = (page: Page) => page.locator('.cm-md-image img').first();
export const imageSurface = (page: Page) => overlay(page).locator('[data-image-regions]');
export const imageTag = (page: Page, number: number) => overlay(page).locator(`[data-region-tag="${number}"]:not([inert])`);

export async function openDocument(page: Page, seeded: { sessionTitle: string }, width: number) {
	await loginByUrl(page);
	if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(seeded.sessionTitle).first().click();
	await expect(docImage(page)).toBeVisible();
	await expect.poll(() => docImage(page).evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(DOC_IMAGE_NATURAL.width);
	await turnOffDocumentComments(page);
}

/** Opens the document's image in the viewer and waits until it has drawn and fitted. */
export async function openImage(page: Page, which = 0) {
	await page.locator('.cm-md-image img').nth(which).click();
	await expect(overlay(page)).toBeVisible();
	const img = overlay(page).locator('.frame img');
	await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(DOC_IMAGE_NATURAL.width);
	let last = '';
	await expect.poll(async () => {
		const now = JSON.stringify(await imageSurface(page).boundingBox());
		const same = now === last;
		last = now;
		return same;
	}, { intervals: [150] }).toBe(true);
}

/** Draws a box over part of the image and posts a comment on it; the drag starts on bare image, not on a tag. */
export async function commentOnRegion(page: Page, text: string, at = { x: 0.2, y: 0.25 }) {
	await bar(page).locator('[data-image-comment]').click();
	const box = (await imageSurface(page).boundingBox())!;
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
export async function putSheetAway(page: Page) {
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

/** Every settled bar control is whole; the phone's comments pill may reflow to a second row. */
export async function expectBarFits(page: Page) {
	// inlineSlot deliberately clips its child while the slot grows; measure after that entrance completes.
	await bar(page).evaluate(async (element) => {
		await Promise.all(element.getAnimations({ subtree: true }).map((animation) => animation.finished.catch(() => undefined)));
	});
	const barBox = (await bar(page).boundingBox())!;
	const view = (await overlay(page).boundingBox())!;
	expect(barBox.x + barBox.width, 'the bar ends inside the viewer').toBeLessThanOrEqual(view.x + view.width + 0.5);
	expect(barBox.height, 'the bar fits within two phone rows').toBeLessThan(100);
	for (const control of await bar(page).locator('button').all()) {
		if (!(await control.isVisible())) continue;
		const box = (await control.boundingBox())!;
		expect(box.x + box.width, `${await control.getAttribute('aria-label') ?? await control.innerText()} ends inside the bar`).toBeLessThanOrEqual(barBox.x + barBox.width + 0.5);
	}
	await expect(bar(page).getByRole('button', { name: 'Fit', exact: true })).toBeInViewport({ ratio: 1 });
}
