import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, type APIRequestContext, type Locator, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from './corsia';
import { syntheticPng } from './syntheticImages';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
export const NATURAL = { width: 1600, height: 1000 };
const tok = () => encodeURIComponent(TEST_TOKEN);

export interface Seeded { sessionId: string; sessionTitle: string; imagePath: string; artifactId: string }
export type Region = { x: number; y: number; w: number; h: number };
type Box = { x: number; y: number; width: number; height: number };

/** Runs a short script against the isolated E2E database; only synthetic mock state may be touched. */
export function isolatedDb(script: string[], ...args: string[]): string {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) throw new Error('Requires isolated E2E mock state');
	return execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', ['import json, sqlite3, sys', 'connection = sqlite3.connect(sys.argv[1])', ...script, 'connection.commit()', 'connection.close()'].join('\n'),
		path.join(stateRoot, 'corsia.db'), ...args,
	], { cwd: repoRoot, encoding: 'utf8' });
}

/** Marks a seeded comment as an agent's, written by the image's own thread. */
export function asAgent(seeded: Seeded, commentId: string) {
	isolatedDb([
		'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Reviewer", sys.argv[2], sys.argv[3])).rowcount',
		'assert changed == 1, changed',
	], seeded.sessionId, commentId);
}

/** An image artifact open in the canvas of its own thread; `png` replaces the synthetic gradient. */
export async function seedImage(page: Page, request: APIRequestContext, label: string, artifactWidth: number | null = null, png?: Buffer): Promise<Seeded> {
	const stamp = Date.now();
	const sessionTitle = `${label}-${stamp}`;
	const sessionId = await createSession(request, sessionTitle);
	const tree = await (await request.get(`/api/files/tree?token=${tok()}`)).json();
	const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
	const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
	expect(workspaceRoot).toEqual(expect.any(String));
	const imagePath = path.join(workspaceRoot, `${label}-${stamp}.png`);
	fs.writeFileSync(imagePath, png ?? syntheticPng(NATURAL.width, NATURAL.height));
	const registered = await request.post(`/api/files/register?token=${tok()}`, { data: { path: imagePath, thread_id: sessionId } });
	expect(registered.ok(), `register failed: ${registered.status()}`).toBeTruthy();
	const artifactId = (await registered.json()).artifact.id as string;
	await page.addInitScript(
		({ threadId, id, tabPath, title, width }) => {
			localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
				threads: { [threadId]: {
					tabs: [{ instanceId: 'tab-image', artifactId: id, path: tabPath, title, loadedRevision: null, latestRevision: null, status: 'loading' }],
					activeTabId: 'tab-image', visible: true, activeSurface: 'artifact',
				} },
				splitWidth: width,
			}));
		},
		{ threadId: sessionId, id: artifactId, tabPath: imagePath, title: path.basename(imagePath), width: artifactWidth },
	);
	return { sessionId, sessionTitle, imagePath, artifactId };
}

/** A real, busy screen to comment on: the signed-in app itself at the image's natural size. */
export async function appScreenshotPng(page: Page): Promise<Buffer> {
	await page.setViewportSize(NATURAL);
	await loginByUrl(page);
	// The shell fades in; a capture mid-fade is a near-blank page, not the busy screen the dense test needs.
	await page.evaluate(() => Promise.allSettled(document.getAnimations()
		.filter((animation) => animation.effect?.getTiming().iterations !== Infinity)
		.map((animation) => animation.finished)));
	return page.screenshot({ fullPage: false, animations: 'disabled' });
}

export async function showImage(page: Page, title: string, naturalWidth = NATURAL.width) {
	await page.getByText(title).first().click();
	// This thread's own image, named for it: the previous thread's viewer can still be on screen with the same size.
	const img = page.locator(`.image-viewer .frame img[src*="${encodeURIComponent(`${title}.png`)}"]`);
	await expect(img).toBeVisible();
	await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(naturalWidth);
	// The frame lays out wider than the viewer first and fits once the panel settles; measuring before that aims off the image.
	let last = '';
	await expect.poll(async () => {
		const [box, view] = await Promise.all([frame(page), viewer(page).boundingBox()]);
		const now = JSON.stringify(box);
		const same = now === last && !!view && inside(box, view);
		last = now;
		return same;
	}, { intervals: [150] }).toBe(true);
}
export async function openThread(page: Page, title: string, width: number) {
	await loginByUrl(page);
	if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await showImage(page, title);
}

export const viewer = (page: Page) => page.locator('.image-viewer');
export const frame = async (page: Page) => (await page.locator('.image-viewer [data-image-regions]').boundingBox())!;
export const at = async (page: Page, fx: number, fy: number) => {
	const box = await frame(page);
	return { x: box.x + box.width * fx, y: box.y + box.height * fy };
};
// A tag or mark that is leaving stays in the page, inert, while it fades out; only the live ones are read.
export const tag = (page: Page, number: number) => page.locator(`.image-viewer [data-region-tag="${number}"]:not([inert])`);
export const tagNumbers = (page: Page) => page.locator('.image-viewer [data-region-tag]:not([inert])').evaluateAll((tags) => tags.map((t) => (t as HTMLElement).dataset.regionTag));
export const commentButton = (page: Page) => viewer(page).locator('[data-image-comment]');
export const pill = (page: Page) => viewer(page).locator('[data-image-comments-pill]');
export const fitButton = (page: Page) => viewer(page).getByRole('button', { name: 'Fit', exact: true });
export const composer = (page: Page) => page.locator('textarea[placeholder="Comment…"]');
export const drawer = (page: Page) => page.locator('.canvas-comment-drawer');
export const drawerList = (page: Page) => drawer(page).locator('.drawer-comments');
export const sheet = (page: Page) => page.locator('.drawer-panel[role="dialog"]');
export const toast = (page: Page, text: string) => page.locator('.toast').filter({ hasText: text });
export const overlap = (a: Box, b: Box) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
export const inside = (inner: Box, outer: Box) => inner.x >= outer.x - 0.5 && inner.y >= outer.y - 0.5
	&& inner.x + inner.width <= outer.x + outer.width + 0.5 && inner.y + inner.height <= outer.y + outer.height + 0.5;

/** Rests the pointer on an empty part of the image, so no tag is hovered. */
export const rest = async (page: Page) => {
	const empty = await at(page, 0.97, 0.97);
	await page.mouse.move(empty.x, empty.y);
};
export async function drag(page: Page, from: { x: number; y: number }, to: { x: number; y: number }) {
	await page.mouse.move(from.x, from.y);
	await page.mouse.down();
	await page.mouse.move(to.x, to.y, { steps: 8 });
	await page.mouse.up();
}
/** One finger, through CDP, so the touch path (not the mouse path) draws or pans. */
export async function touchDrag(page: Page, a: { x: number; y: number }, b: { x: number; y: number }) {
	const cdp = await page.context().newCDPSession(page);
	await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: a.x, y: a.y, id: 0 }] });
	for (let step = 1; step <= 8; step++) {
		await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: a.x + ((b.x - a.x) * step) / 8, y: a.y + ((b.y - a.y) * step) / 8, id: 0 }] });
	}
	await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
	await cdp.detach();
}

export async function shoot(page: Page, testInfo: TestInfo, name: string, both = false) {
	if (both) {
		await page.emulateMedia({ colorScheme: 'light' });
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
		await saveEvidenceScreenshot(page, testInfo, `${name}-light`, { fullPage: false });
	}
	await page.emulateMedia({ colorScheme: 'dark' });
	await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
	await saveEvidenceScreenshot(page, testInfo, `${name}-dark`, { fullPage: false });
}
/** A plain frame that never fast-forwards a running transition. */
export async function still(page: Page, testInfo: TestInfo, name: string, clip?: Box) {
	await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: false, animations: 'allow', ...(clip ? { clip } : {}) });
}

/** The area to film: `around`'s box, padded, inside the viewport. */
async function clipOf(page: Page, around: Locator): Promise<Box> {
	const box = await around.boundingBox();
	expect(box, 'the filmed element has a box').not.toBeNull();
	const view = page.viewportSize()!;
	const x = Math.max(0, box!.x - 12), y = Math.max(0, box!.y - 12);
	return { x, y, width: Math.min(view.width, box!.x + box!.width + 12) - x, height: Math.min(view.height, box!.y + box!.height + 12) - y };
}

/**
 * Films one transition. Every animation in the page runs at `rate` from before the trigger (CDP
 * Animation.setPlaybackRate), so the frames land inside it, and each frame reads `probe` (the changed
 * element's own property), so an unrelated animation elsewhere cannot pass for this one. Frames go to
 * `<name>-fNN.png`, the probe readings to `<name>.json`.
 */
export async function record(page: Page, testInfo: TestInfo, name: string, around: Locator, trigger: () => Promise<unknown>,
	probe: () => Promise<string | number>, { rate = 0.1, frames = 10, every = 120 } = {}): Promise<Array<string | number>> {
	const clip = await clipOf(page, around);
	const cdp = await page.context().newCDPSession(page);
	const values: Array<string | number> = [];
	try {
		await cdp.send('Animation.enable');
		await cdp.send('Animation.setPlaybackRate', { playbackRate: rate });
		values.push(await probe());
		await still(page, testInfo, `${name}-f00`, clip);
		await trigger();
		// A trigger that detaches its own CDP session (touchDrag) puts the clock back to 1, so set it again.
		await cdp.send('Animation.setPlaybackRate', { playbackRate: rate });
		for (let index = 1; index <= frames; index++) {
			values.push(await probe());
			await still(page, testInfo, `${name}-f${String(index).padStart(2, '0')}`, clip);
			await page.waitForTimeout(every);
		}
	} finally {
		await cdp.send('Animation.setPlaybackRate', { playbackRate: 1 });
		await cdp.detach();
	}
	fs.writeFileSync(testInfo.outputPath(`${name}.json`), JSON.stringify(values));
	return values;
}

/**
 * Films a smooth scroll, which the animation clock does not slow: a page-side sampler reads the scrolled
 * element every frame while frames are taken as fast as they come. Returns the distinct positions seen.
 */
export async function recordScroll(page: Page, testInfo: TestInfo, name: string, around: Locator, scroller: Locator,
	trigger: () => Promise<unknown>, { frames = 6, ms = 900 } = {}): Promise<number[]> {
	const clip = await clipOf(page, around);
	await scroller.evaluate((el, duration) => {
		const seen: number[] = [];
		(window as unknown as { __scrollSeen: number[] }).__scrollSeen = seen;
		const start = performance.now();
		const tick = () => {
			seen.push(Math.round(el.scrollLeft + el.scrollTop));
			if (performance.now() - start < duration) requestAnimationFrame(tick);
		};
		requestAnimationFrame(tick);
	}, ms);
	await still(page, testInfo, `${name}-f00`, clip);
	await trigger();
	for (let index = 1; index <= frames; index++) await still(page, testInfo, `${name}-f${String(index).padStart(2, '0')}`, clip);
	await page.waitForTimeout(ms);
	const seen = await page.evaluate(() => (window as unknown as { __scrollSeen: number[] }).__scrollSeen);
	const distinct = seen.filter((value, index) => index === 0 || value !== seen[index - 1]);
	fs.writeFileSync(testInfo.outputPath(`${name}.json`), JSON.stringify(distinct));
	return distinct;
}

/** A filmed transition moved through at least one in-between state from where it started to somewhere else. */
export function expectTransition(values: Array<string | number>, what: string) {
	expect(values[0], `${what} changed`).not.toEqual(values[values.length - 1]);
	expect(new Set(values).size, `${what} passed through an in-between state: ${JSON.stringify(values)}`).toBeGreaterThanOrEqual(3);
}

/** Holds a press on `target` and frames it; the release's click is swallowed so the next state is unchanged. */
export async function pressFrame(page: Page, target: Locator, testInfo: TestInfo, name: string, read: (el: Element) => string) {
	const box = await target.boundingBox();
	expect(box, `${name} has a box`).not.toBeNull();
	await page.evaluate(() => window.addEventListener('click', (event) => {
		event.preventDefault();
		event.stopImmediatePropagation();
	}, { capture: true, once: true }));
	// Compared with the settled hover, so a hover tint can never pass for the press.
	await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
	await page.waitForTimeout(400);
	const atRest = await target.evaluate(read);
	await page.mouse.down();
	const held = await target.evaluate(read);
	await still(page, testInfo, name, await clipOf(page, target));
	await page.mouse.up();
	expect(held, `${name} looks pressed while held`).not.toBe(atRest);
}

/** Seeds a comment through the disposable mock API, for many-of-N evidence only. */
export async function apiComment(request: APIRequestContext, artifactId: string, body: string, region?: Region): Promise<string> {
	const response = await request.post(`/api/artifacts/${artifactId}/comments?token=${tok()}`, { data: { body, ...(region ? { region } : {}) } });
	expect(response.ok(), `comment seed failed: ${response.status()}`).toBeTruthy();
	return (await response.json()).comment.id as string;
}

export async function postComment(page: Page, text: string) {
	await expect(composer(page)).toBeVisible();
	await composer(page).fill(text);
	await page.keyboard.press('ControlOrMeta+Enter');
	await expect(composer(page)).toHaveCount(0);
}

/** Replaces the image with a new revision, so every pin placed before reads stale. */
export function newRevision(seeded: Seeded) {
	fs.writeFileSync(seeded.imagePath, syntheticPng(NATURAL.width, NATURAL.height + 2));
}

/**
 * Sends the open comments drawer's batch with Send now from the keyboard (R2: focus unchanged). Once the footer has gone,
 * focus rests on the drawer's heading with its ring, still inside the drawer, and Escape then closes only the drawer:
 * `remains`, on the canvas under it, stays. `name` shoots that rest in both themes. Returns the send request.
 */
export async function sendFromDrawerByKeyboard(page: Page, testInfo: TestInfo, remains: Locator, name?: string) {
	const footer = page.locator('[data-doc-comments-drawer] [data-drawer-unsent]');
	await footer.getByRole('button', { name: 'Send now', exact: true }).focus();
	const sent = page.waitForRequest((req) => req.method() === 'POST' && req.url().includes('/api/doc-feedback/send'));
	await page.keyboard.press('Enter');
	const request = await sent;
	await expect(footer).toHaveCount(0);
	const home = page.locator('[data-doc-comments-drawer] [data-drawer-focus-home]');
	await expect(home).toBeFocused();
	expect(await home.evaluate((el) => el.matches(':focus-visible')), 'the heading shows its focus ring').toBe(true);
	if (name) await shoot(page, testInfo, name, true);
	await expect(home).toBeFocused();
	await page.keyboard.press('Escape');
	await expect(page.locator('[data-doc-comments-drawer]')).toHaveCount(0);
	await expect(remains).toBeVisible();
	return request;
}
