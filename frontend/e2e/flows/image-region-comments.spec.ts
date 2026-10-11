import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';
import { syntheticPng } from '../support/syntheticImages';
import { openSuggestionDocument } from '../support/docSuggestions';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const NATURAL = { width: 1600, height: 1000 };

interface Seeded { sessionId: string; sessionTitle: string; imagePath: string; artifactId: string }

/** Runs a short script against the isolated E2E database; only synthetic mock state may be touched. */
function isolatedDb(script: string[], ...args: string[]): string {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) throw new Error('Requires isolated E2E mock state');
	return execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', ['import json, sqlite3, sys', 'connection = sqlite3.connect(sys.argv[1])', ...script, 'connection.commit()', 'connection.close()'].join('\n'),
		path.join(stateRoot, 'corsia.db'), ...args,
	], { cwd: repoRoot, encoding: 'utf8' });
}

/** An image artifact open in the canvas of its own thread. */
async function seedImage(page: Page, request: APIRequestContext, label: string, artifactWidth: number | null = null): Promise<Seeded> {
	const stamp = Date.now();
	const sessionTitle = `${label}-${stamp}`;
	const sessionId = await createSession(request, sessionTitle);
	const tok = encodeURIComponent(TEST_TOKEN);
	const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
	const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
	const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
	expect(workspaceRoot).toEqual(expect.any(String));
	const imagePath = path.join(workspaceRoot, `${label}-${stamp}.png`);
	fs.writeFileSync(imagePath, syntheticPng(NATURAL.width, NATURAL.height));
	const registered = await request.post(`/api/files/register?token=${tok}`, { data: { path: imagePath, thread_id: sessionId } });
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

async function showImage(page: Page, title: string, naturalWidth = NATURAL.width) {
	await page.getByText(title).first().click();
	const img = page.locator('.image-viewer .frame img');
	await expect(img).toBeVisible();
	await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(naturalWidth);
}
async function openThread(page: Page, title: string, width: number) {
	await loginByUrl(page);
	if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await showImage(page, title);
}

const viewer = (page: Page) => page.locator('.image-viewer');
const frame = async (page: Page) => (await page.locator('.image-viewer [data-image-regions]').boundingBox())!;
const at = async (page: Page, fx: number, fy: number) => {
	const box = await frame(page);
	return { x: box.x + box.width * fx, y: box.y + box.height * fy };
};
const tag = (page: Page, number: number) => page.locator(`.image-viewer [data-region-tag="${number}"]`);
const tagNumbers = (page: Page) => page.locator('.image-viewer [data-region-tag]').evaluateAll((tags) => tags.map((t) => (t as HTMLElement).dataset.regionTag));
const commentButton = (page: Page) => viewer(page).locator('[data-image-comment]');
const pill = (page: Page) => viewer(page).locator('[data-image-comments-pill]');
const composer = (page: Page) => page.locator('textarea[placeholder="Comment…"]');
type Box = { x: number; y: number; width: number; height: number };
const overlap = (a: Box, b: Box) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
/** Rests the pointer on an empty part of the image, so no tag is hovered. */
const rest = async (page: Page) => {
	const empty = await at(page, 0.95, 0.95);
	await page.mouse.move(empty.x, empty.y);
};

async function shoot(page: Page, testInfo: Parameters<typeof saveEvidenceScreenshot>[1], name: string, both = false) {
	if (both) {
		await page.emulateMedia({ colorScheme: 'light' });
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
		await saveEvidenceScreenshot(page, testInfo, `${name}-light`, { fullPage: false });
	}
	await page.emulateMedia({ colorScheme: 'dark' });
	await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
	await saveEvidenceScreenshot(page, testInfo, `${name}-dark`, { fullPage: false });
}

/** Screenshots a paused transition without Playwright fast-forwarding it. */
async function shootMotion(page: Page, testInfo: Parameters<typeof saveEvidenceScreenshot>[1], name: string) {
	await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: false, animations: 'allow' });
}
/** Captures an exact start/mid/end triplet, or records the end state when the interaction is instant. */
async function motionFrames(page: Page, testInfo: Parameters<typeof saveEvidenceScreenshot>[1], name: string): Promise<'animated' | 'instant'> {
	const count = await page.evaluate(() => {
		const animations = document.getAnimations();
		for (const animation of animations) animation.pause();
		return animations.length;
	});
	if (!count) { await shootMotion(page, testInfo, `${name}-instant`); return 'instant'; }
	for (const [state, fraction] of [['start', 0], ['mid', .5], ['end', 1]] as const) {
		await page.evaluate((ratio) => {
			for (const animation of document.getAnimations()) {
				const duration = animation.effect?.getComputedTiming().activeDuration ?? 0;
				animation.currentTime = duration * ratio;
			}
		}, fraction);
		await shootMotion(page, testInfo, `${name}-${state}`);
	}
	return 'animated';
}

async function postComment(page: Page, text: string) {
	await expect(composer(page)).toBeVisible();
	await composer(page).fill(text);
	await page.keyboard.press('ControlOrMeta+Enter');
	await expect(composer(page)).toHaveCount(0);
}

/** Records native pressed feedback without letting the release click change the state captured next. */
async function pressFrame(page: Page, target: Locator, testInfo: Parameters<typeof saveEvidenceScreenshot>[1], name: string) {
	const box = await target.boundingBox();
	expect(box, `${name} has a box`).not.toBeNull();
	await page.evaluate(() => window.addEventListener('click', (event) => {
		event.preventDefault();
		event.stopImmediatePropagation();
	}, { capture: true, once: true }));
	await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
	await page.mouse.down();
	await shootMotion(page, testInfo, name);
	await page.mouse.up();
}

/** Seed a comment through the disposable mock API, bypassing UI repetition only for many-of-N evidence. */
async function apiComment(request: APIRequestContext, artifactId: string, body: string, region?: { x: number; y: number; w: number; h: number }): Promise<string> {
	const tok = encodeURIComponent(TEST_TOKEN);
	const response = await request.post(`/api/artifacts/${artifactId}/comments?token=${tok}`, { data: { body, ...(region ? { region } : {}) } });
	expect(response.ok(), `comment seed failed: ${response.status()}`).toBeTruthy();
	return (await response.json()).comment.id as string;
}

test.describe('@flow:image-region-comments @surface:documents comments on parts of an image', () => {
	test.use({ hasTouch: true });

	test('desktop: draw, number, reply, resolve, list, deliver, agent tag and stale pins (1440)', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(180_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.emulateMedia({ colorScheme: 'dark' });
		const seeded = await seedImage(page, request, 'region-1440');
		try {
			await openThread(page, seeded.sessionTitle, 1440);
			await expect(pill(page)).toHaveAttribute('aria-label', '0 comments');
			await expect(commentButton(page)).toBeVisible();
			await shoot(page, testInfo, '1440-01-open', true);

			// A mouse drag draws a box anywhere on the image; the composer opens beside it.
			const from = await at(page, 0.2, 0.25);
			const to = await at(page, 0.45, 0.5);
			await page.mouse.move(from.x, from.y);
			await page.mouse.down();
			await page.mouse.move(to.x, to.y, { steps: 8 });
			await page.mouse.up();
			await expect(composer(page)).toBeVisible();
			await composer(page).fill('The header is too tall here');
			await shoot(page, testInfo, '1440-02-composer-box', true);
			await page.keyboard.press('ControlOrMeta+Enter');
			await expect(composer(page)).toHaveCount(0);
			await expect(tag(page, 1)).toBeVisible();
			const card = page.locator('.ct').filter({ hasText: 'The header is too tall here' });
			await expect(card).toBeVisible();
			await expect(card.locator('.ct-num')).toHaveText('1');
			// In a narrow canvas the card falls below, never over the tag or the box it is about.
			const cardBox = (await card.boundingBox())!;
			expect(overlap((await tag(page, 1).boundingBox())!, cardBox)).toBe(false);
			expect(overlap((await page.locator('.image-viewer [data-region-box]').boundingBox())!, cardBox)).toBe(false);
			await expect(tag(page, 1)).not.toHaveClass(/faint/);
			await shoot(page, testInfo, '1440-03-card-beside-tag', true);

			// Escape closes the card; the tags go faint until hovered.
			await page.keyboard.press('Escape');
			await expect(card).toHaveCount(0);
			await rest(page);
			await expect(tag(page, 1)).toHaveClass(/faint/);

			// A click is a point.
			const point = await at(page, 0.72, 0.3);
			await page.mouse.click(point.x, point.y);
			await postComment(page, 'This icon reads as disabled');
			await expect(tag(page, 2)).toBeVisible();
			await page.keyboard.press('Escape');
			await rest(page);
			await expect(tag(page, 2)).toHaveClass(/faint/);
			await tag(page, 1).click();
			const firstCardWithNeighbours = page.locator('.ct').filter({ hasText: 'The header is too tall here' });
			await expect(firstCardWithNeighbours).toBeVisible();
			const neighbouringCardBox = (await firstCardWithNeighbours.boundingBox())!;
			for (const regionTag of await page.locator('.image-viewer [data-region-tag]').all()) {
				expect(overlap((await regionTag.boundingBox())!, neighbouringCardBox)).toBe(false);
			}
			await page.keyboard.press('Escape');
			await shoot(page, testInfo, '1440-04-tags-faint', true);
			await tag(page, 1).hover();
			await expect(tag(page, 1)).not.toHaveClass(/faint/);
			await expect(page.locator('.image-viewer [data-region-box]')).toHaveCount(1);
			await shoot(page, testInfo, '1440-05-tag-hover');

			// Reply on the point.
			await tag(page, 2).click();
			const pointCard = page.locator('.ct').filter({ hasText: 'This icon reads as disabled' });
			await expect(pointCard).toBeVisible();
			await pointCard.locator('input[placeholder="Reply…"]').fill('Agreed, it needs more contrast');
			await pointCard.getByRole('button', { name: 'Send reply' }).click();
			await expect(pointCard.getByText('Agreed, it needs more contrast')).toBeVisible();
			await shoot(page, testInfo, '1440-06-reply', true);
			await page.keyboard.press('Escape');

			// Resolve the box; its number is never handed out again.
			await tag(page, 1).click();
			await card.getByRole('button', { name: 'Resolve' }).click();
			await page.keyboard.press('Escape');
			await expect(tag(page, 1)).toHaveCount(0);
			await commentButton(page).click();
			await expect(commentButton(page)).toHaveAttribute('aria-pressed', 'true');
			await commentButton(page).hover();
			const activeCommentColor = await commentButton(page).evaluate((el) => getComputedStyle(el).color);
			const inactiveZoom = page.locator('.image-viewer:visible .zoom button[aria-pressed="false"]');
			await inactiveZoom.hover();
			expect(await inactiveZoom.evaluate((el) => getComputedStyle(el).color)).not.toBe(activeCommentColor);
			await shoot(page, testInfo, '1440-07-box-mode');
			const surface = page.locator('.image-viewer [data-image-regions] .surface');
			await expect(surface).toBeFocused();
			await page.keyboard.press('Enter');
			await expect(commentButton(page)).toHaveAttribute('aria-pressed', 'false');
			await postComment(page, 'The whole layout feels cramped');
			await page.keyboard.press('Escape');
			expect(await tagNumbers(page)).toEqual(['2', '3']);
			await shoot(page, testInfo, '1440-08-number-not-reused', true);

			// The viewer sits directly above this drawer, so its thumbnail would repeat the image and hide the first card.
			await expect(pill(page)).toHaveAttribute('aria-label', '3 comments');
			await pill(page).click();
			const drawer = page.locator('.canvas-comment-drawer');
			await expect(drawer).toBeVisible();
			await pill(page).hover();
			const activePillColor = await pill(page).evaluate((el) => getComputedStyle(el).color);
			const inactiveZoomForPill = page.locator('.image-viewer:visible .zoom button[aria-pressed="false"]');
			await inactiveZoomForPill.hover();
			expect(await inactiveZoomForPill.evaluate((el) => getComputedStyle(el).color)).not.toBe(activePillColor);
			await expect(drawer.locator('[data-region-thumb]')).toHaveCount(0);
			const firstCard = drawer.locator('.ct').first();
			await expect(firstCard).toBeVisible();
			const [drawerBox, firstCardBox] = await Promise.all([drawer.boundingBox(), firstCard.boundingBox()]);
			expect(firstCardBox!.y).toBeGreaterThanOrEqual(drawerBox!.y);
			expect(firstCardBox!.y + firstCardBox!.height).toBeLessThanOrEqual(drawerBox!.y + drawerBox!.height);
			await shoot(page, testInfo, '1440-09-drawer-open', true);
			await drawer.locator('[data-drawer-filter]', { hasText: 'Resolved' }).click();
			await expect(drawer.locator('.ct')).toHaveCount(1);
			await shoot(page, testInfo, '1440-09b-drawer-resolved');
			await page.keyboard.press('Escape');
			await expect(drawer).toHaveCount(0);

			// Keyboard reach: Tab from the image reaches a tag, Enter opens its card.
			await page.locator('.image-viewer .viewport').focus();
			await page.keyboard.press('Tab');
			await expect(tag(page, 2)).toBeFocused();
			await page.keyboard.press('Enter');
			await expect(pointCard).toBeVisible();
			await shoot(page, testInfo, '1440-10-keyboard-tag-card', true);
			await page.keyboard.press('Escape');
			await expect(tag(page, 2)).toBeFocused();

			// The person's comments are delivered as one batch carrying each region and one marked-up copy.
			isolatedDb([
				'changed = connection.execute("UPDATE doc_feedback_batches SET closes_at=? WHERE state=\'open\' AND author_kind=\'user\' AND artifact_id=?", ("2000-01-01T00:00:00+00:00", sys.argv[2])).rowcount',
				'assert changed == 1, changed',
			], seeded.artifactId);
			let delivered: { title: string; description: string } | null = null;
			await expect.poll(() => {
				delivered = JSON.parse(isolatedDb([
					'row = connection.execute("SELECT n.title,n.description FROM doc_feedback_batches b JOIN plan_nodes n ON n.id=b.delivered_node_id WHERE b.artifact_id=? AND b.state=\'delivered\' ORDER BY b.updated_at DESC LIMIT 1", (sys.argv[2],)).fetchone()',
					'print(json.dumps(dict(zip(("title", "description"), row)) if row else None))',
				], seeded.artifactId));
				return delivered !== null;
			}, { timeout: 75_000, intervals: [1000] }).toBe(true);
			const description = (delivered as unknown as { description: string }).description;
			expect(description).toContain('Region 2: point at x 72%, y 30%');
			expect(description).toContain('Region 3: box at x 0%, y 0%, 100% wide, 100% tall');
			const marked = description.match(/Marked-up image \(numbered boxes\): (\S+\.png)/);
			expect(marked, description).not.toBeNull();
			expect(fs.existsSync(marked![1])).toBe(true);
			fs.copyFileSync(marked![1], testInfo.outputPath('1440-11-delivered-marked-up-image.png'));

			// An agent's comment carries its name beside the number.
			const tok = encodeURIComponent(TEST_TOKEN);
			const agentComment = await request.post(`/api/artifacts/${seeded.artifactId}/comments?token=${tok}`, {
				data: { body: 'The call-to-action is clipped at this width.', region: { x: 0.55, y: 0.6, w: 0.3, h: 0.25 } },
			});
			expect(agentComment.ok(), await agentComment.text()).toBeTruthy();
			const agentId = (await agentComment.json()).comment.id as string;
			isolatedDb([
				'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Reviewer", sys.argv[3], sys.argv[2])).rowcount',
				'assert changed == 1, changed',
			], agentId, seeded.sessionId);
			await page.reload();
			await showImage(page, seeded.sessionTitle);
			await expect(tag(page, 4)).toBeVisible();
			await expect(tag(page, 4)).toContainText('Reviewer');
			await shoot(page, testInfo, '1440-12-agent-tag', true);

			// A new revision of the image dims the pins placed on the old one.
			fs.writeFileSync(seeded.imagePath, syntheticPng(NATURAL.width, NATURAL.height + 2));
			await page.reload();
			await showImage(page, seeded.sessionTitle);
			await expect(tag(page, 2)).toHaveClass(/stale/);
			await expect(tag(page, 4)).toHaveClass(/stale/);
			await expect(tag(page, 2)).toHaveAttribute('aria-label', /image changed since this comment/);
			await expect(tag(page, 2).locator('.disc')).toHaveCSS('border-style', 'dashed');
			expect(await tag(page, 2).evaluate((el) => getComputedStyle(el).opacity)).toBe('1');
			await shoot(page, testInfo, '1440-13-stale-pins', true);

			// Reduced motion: the number's emphasis does not animate (the app-wide rule leaves at most 0.01 ms).
			await page.emulateMedia({ reducedMotion: 'reduce' });
			const duration = await tag(page, 2).locator('.disc').evaluate((el) => getComputedStyle(el).transitionDuration);
			expect(Number.parseFloat(duration)).toBeLessThan(0.001);
		} finally {
			fs.rmSync(seeded.imagePath, { force: true });
		}
	});

	test('wide canvas: the card opens beside the box (1440)', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.emulateMedia({ colorScheme: 'dark' });
		const seeded = await seedImage(page, request, 'region-wide', 1200);
		try {
			await openThread(page, seeded.sessionTitle, 1440);
			// Room for a card beside the box: hide the sidebar and the panel so the canvas takes the width.
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await page.getByRole('button', { name: 'Toggle panel' }).click();
			await expect.poll(async () => (await viewer(page).boundingBox())!.width).toBeGreaterThan(800);
			await expect(pill(page)).toHaveAttribute('aria-label', '0 comments');
			// The fit settles after the canvas widens; draw on the settled image.
			let last = 0;
			await expect.poll(async () => { const w = (await frame(page)).width; const settled = w === last; last = w; return settled; }).toBe(true);
			const from = await at(page, 0.15, 0.3);
			const to = await at(page, 0.35, 0.55);
			await page.mouse.move(from.x, from.y);
			await page.mouse.down();
			await page.mouse.move(to.x, to.y, { steps: 8 });
			await page.mouse.up();
			await postComment(page, 'The header is too tall here');
			const card = page.locator('.ct').filter({ hasText: 'The header is too tall here' });
			await expect(card).toBeVisible();
			const cardBox = (await card.boundingBox())!;
			const box = (await page.locator('.image-viewer [data-region-box]').boundingBox())!;
			expect(overlap(box, cardBox)).toBe(false);
			expect(overlap((await tag(page, 1).boundingBox())!, cardBox)).toBe(false);
			// Beside: the card shares the box's rows instead of dropping below it.
			expect(cardBox.y).toBeLessThan(box.y + box.height);
			await shoot(page, testInfo, '1440-14-wide-card-beside-box', true);
		} finally {
			fs.rmSync(seeded.imagePath, { force: true });
		}
	});

	test('phone: the Comment button draws one box with one finger, in a bottom sheet (393)', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		await page.setViewportSize({ width: 393, height: 852 });
		await page.emulateMedia({ colorScheme: 'dark' });
		const seeded = await seedImage(page, request, 'region-393');
		try {
			await openThread(page, seeded.sessionTitle, 393);
			expect((await commentButton(page).boundingBox())!.height).toBeGreaterThanOrEqual(44);
			expect((await pill(page).boundingBox())!.height).toBeGreaterThanOrEqual(44);
			await shoot(page, testInfo, '393-01-open', true);

			const cdp = await page.context().newCDPSession(page);
			const drag = async (a: { x: number; y: number }, b: { x: number; y: number }) => {
				await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: a.x, y: a.y, id: 0 }] });
				for (let step = 1; step <= 8; step++) {
					await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: a.x + ((b.x - a.x) * step) / 8, y: a.y + ((b.y - a.y) * step) / 8, id: 0 }] });
				}
				await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
			};

			await commentButton(page).tap();
			await expect(commentButton(page)).toHaveAttribute('aria-pressed', 'true');
			await shoot(page, testInfo, '393-02-box-mode', true);
			await drag(await at(page, 0.2, 0.3), await at(page, 0.6, 0.6));
			const sheet = page.locator('.drawer-panel[role="dialog"]');
			await expect(sheet).toBeVisible();
			await expect(sheet.locator('textarea')).toBeVisible();
			await sheet.locator('textarea').fill('Too much padding on phones');
			await shoot(page, testInfo, '393-03-sheet-composer', true);
			await sheet.getByRole('button', { name: 'Comment', exact: true }).tap();
			await expect(sheet.locator('.ct-num')).toHaveText('1');
			// Box mode ended after one comment.
			await expect(commentButton(page)).toHaveAttribute('aria-pressed', 'false');
			await shoot(page, testInfo, '393-04-sheet-card', true);
			await page.keyboard.press('Escape');
			await expect(sheet).toHaveCount(0);

			// Outside box mode one finger pans again: nothing is drawn.
			await drag(await at(page, 0.7, 0.7), await at(page, 0.5, 0.5));
			await expect(composer(page)).toHaveCount(0);

			// The tag's touch target is 44 px around its number.
			const disc = (await tag(page, 1).locator('.disc').boundingBox())!;
			const centre = { x: disc.x + disc.width / 2, y: disc.y + disc.height / 2 };
			for (const [dx, dy] of [[-20, 0], [20, 0], [0, -20], [0, 20]]) {
				expect(await page.evaluate(({ x, y }) => !!document.elementFromPoint(x, y)?.closest('[data-region-tag="1"]'), { x: centre.x + dx, y: centre.y + dy })).toBe(true);
			}
			await tag(page, 1).tap();
			await expect(sheet).toBeVisible();
			await expect(sheet.getByText('Too much padding on phones')).toBeVisible();
			await shoot(page, testInfo, '393-05-tag-opens-sheet', true);
		} finally {
			fs.rmSync(seeded.imagePath, { force: true });
		}
	});

	test('review evidence: dense, neighbouring, motion and move-pin states', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(180_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		const seeded = await seedImage(page, request, 'region-review-evidence', 1200);
		try {
			const regions = [
				{ x: .08, y: .12, w: 0, h: 0 }, { x: .22, y: .16, w: .12, h: .1 }, { x: .44, y: .1, w: 0, h: 0 },
				{ x: .68, y: .14, w: .2, h: .12 }, { x: .91, y: .1, w: .08, h: .15 }, { x: .1, y: .42, w: .16, h: .18 },
				{ x: .37, y: .46, w: 0, h: 0 }, { x: .57, y: .44, w: .14, h: .18 }, { x: .82, y: .48, w: 0, h: 0 },
				{ x: .05, y: .78, w: .16, h: .16 }, { x: .42, y: .8, w: 0, h: 0 }, { x: .78, y: .76, w: .18, h: .17 },
			];
			const ids = await Promise.all(regions.map((region, index) => apiComment(request, seeded.artifactId, `Dense review note ${index + 1}`, region)));
			await apiComment(request, seeded.artifactId, 'Text comment beside the image comments');
			for (const index of [2, 7]) {
				isolatedDb([
					'connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Reviewer", sys.argv[2], sys.argv[3]))',
				], seeded.sessionId, ids[index]);
			}
			await openThread(page, seeded.sessionTitle, 1440);
			await expect(pill(page)).toHaveAttribute('aria-label', '13 comments');
			await rest(page);
			await shoot(page, testInfo, '1440-15-dense-12-tags-rest', true);
			await tag(page, 7).hover();
			await shoot(page, testInfo, '1440-16-dense-12-hover', true);
			await tag(page, 8).focus();
			await page.keyboard.press('Enter');
			await expect(tag(page, 8)).toHaveAttribute('aria-pressed', 'true');
			await expect(page.locator('.ct')).toBeVisible();
			await shoot(page, testInfo, '1440-17-dense-12-selected', true);
			await page.keyboard.press('Escape');
			await pill(page).click();
			const drawer = page.locator('.canvas-comment-drawer');
			await expect(drawer.getByText('Text comment beside the image comments')).toBeVisible();
			await expect(drawer.getByText('Dense review note 1', { exact: true })).toBeVisible();
			await shoot(page, testInfo, '1440-18-text-and-region-neighbours', true);
			await page.keyboard.press('Escape');

			// Responsive interaction mode is selected as the viewer mounts, so remount at phone width.
			await page.setViewportSize({ width: 393, height: 852 });
			await page.reload();
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await showImage(page, seeded.sessionTitle);
			await rest(page);
			await shoot(page, testInfo, '393-06-dense-12-tags-rest', true);
			await tag(page, 8).tap();
			await expect(page.locator('.drawer-panel .ct')).toBeVisible();
			await shoot(page, testInfo, '393-07-dense-12-selected', true);
			await page.keyboard.press('Escape');
			await pill(page).tap();
			const phoneDrawer = page.locator('.canvas-comment-drawer');
			await expect(phoneDrawer.locator('.ct')).toHaveCount(13);
			await shoot(page, testInfo, '393-08-dense-12-drawer', true);
			await page.keyboard.press('Escape');

			await page.setViewportSize({ width: 1440, height: 900 });
			await tag(page, 7).hover();
			expect(await motionFrames(page, testInfo, '1440-19-tag-hover-motion')).toBe('animated');
			await page.emulateMedia({ reducedMotion: 'reduce' });
			expect(Number.parseFloat(await tag(page, 7).locator('.disc').evaluate((el) => getComputedStyle(el).transitionDuration))).toBeLessThan(0.001);
			await shoot(page, testInfo, '1440-20-tag-motion-reduced', true);
			await page.emulateMedia({ reducedMotion: 'no-preference' });

			fs.writeFileSync(seeded.imagePath, syntheticPng(NATURAL.width, NATURAL.height + 2));
			await page.reload();
			await showImage(page, seeded.sessionTitle);
			await tag(page, 1).focus();
			await page.keyboard.press('Enter');
			const staleCard = page.locator('.ct:has([data-move-pin])');
			await expect(staleCard.getByRole('button', { name: 'Move pin' })).toBeVisible();
			let releasePatch: (() => void) | null = null;
			const patchHeld = new Promise<void>((resolve) => { releasePatch = resolve; });
			await page.route('**/api/comments/**', async (route) => {
				if (route.request().method() === 'PATCH') await patchHeld;
				await route.continue();
			});
			await staleCard.getByRole('button', { name: 'Move pin' }).click();
			await motionFrames(page, testInfo, '1440-20b-move-pin-start');
			const moveTo = await at(page, .62, .64);
			await page.mouse.click(moveTo.x, moveTo.y);
			await expect(commentButton(page)).toContainText('Moving pin…');
			await shoot(page, testInfo, '1440-21-move-pin-pending', true);
			releasePatch?.();
			await expect(commentButton(page)).not.toContainText('Moving pin…');
			await expect(tag(page, 1)).not.toHaveClass(/stale/);
			await shoot(page, testInfo, '1440-22-move-pin-settled', true);
		} finally {
			fs.rmSync(seeded.imagePath, { force: true });
		}
	});

	test('review evidence: transition matrix and press feedback (1440)', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(180_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'no-preference' });
		const seeded = await seedImage(page, request, 'region-motion', 1200);
		try {
			await openThread(page, seeded.sessionTitle, 1440);
			await pressFrame(page, commentButton(page), testInfo, '1440-23-press-comment');
			await commentButton(page).click();
			await motionFrames(page, testInfo, '1440-24-comment-box-mode-on');
			await commentButton(page).click();
			await motionFrames(page, testInfo, '1440-25-comment-box-mode-off');
			await commentButton(page).click();
			const from = await at(page, .2, .25);
			const to = await at(page, .45, .5);
			await page.mouse.move(from.x, from.y);
			await page.mouse.down();
			await page.mouse.move(to.x, to.y, { steps: 8 });
			await shootMotion(page, testInfo, '1440-26-draft-drawing-instant');
			await page.mouse.up();
			await expect(composer(page)).toBeVisible();
			await motionFrames(page, testInfo, '1440-27-composer-open');
			await postComment(page, 'A transition-matrix comment');
			const card = page.locator('.ct').filter({ hasText: 'A transition-matrix comment' });
			await expect(card).toBeVisible();
			await motionFrames(page, testInfo, '1440-28-card-open');
			await pressFrame(page, tag(page, 1), testInfo, '1440-29-press-tag');
			await page.keyboard.press('Escape');
			await motionFrames(page, testInfo, '1440-30-card-close');
			let releaseCommentReload: (() => void) | null = null;
			const commentReloadHeld = new Promise<void>((resolve) => { releaseCommentReload = resolve; });
			await page.route(`**/api/artifacts/${seeded.artifactId}/comments**`, async (route) => {
				if (route.request().method() === 'GET') await commentReloadHeld;
				await route.continue();
			});
			const incomingAgent = await apiComment(request, seeded.artifactId, 'An arriving agent pin', { x: .72, y: .3, w: .16, h: .12 });
			await expect.poll(() => Boolean(releaseCommentReload)).toBe(true);
			isolatedDb([
				'connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Reviewer", sys.argv[2], sys.argv[3]))',
			], seeded.sessionId, incomingAgent);
			releaseCommentReload?.();
			const sweep = page.locator(`[data-region-box="${incomingAgent}"]`);
			await expect(sweep).toHaveClass(/sweep/);
			expect(await motionFrames(page, testInfo, '1440-30b-agent-pin-sweep')).toBe('animated');
			await tag(page, 1).hover();
			await motionFrames(page, testInfo, '1440-31-tag-hover');
			await tag(page, 1).focus();
			await page.keyboard.press('Enter');
			await motionFrames(page, testInfo, '1440-32-tag-select');
			await page.keyboard.press('Escape');
			await pressFrame(page, pill(page), testInfo, '1440-33-press-pill');
			await pill(page).click();
			await expect(page.locator('.canvas-comment-drawer')).toBeVisible();
			await motionFrames(page, testInfo, '1440-34-drawer-open');
			await page.keyboard.press('Escape');
			await motionFrames(page, testInfo, '1440-35-drawer-close');
		} finally {
			fs.rmSync(seeded.imagePath, { force: true });
		}
	});

	test('review evidence: text comment neighbour card and phone composer', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(120_000);
		const text = '# Review target\n\nThe text comment is beside the image-comment surface.';
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const desktop = await openSuggestionDocument(page, request, text, [{ quote: 'text comment is beside', body: 'Text review note beside an image comment.' }]);
		const marker = page.locator(`.cm-comment-marker[data-comment-ids*="${desktop.ids[0]}"]`);
		await marker.click();
		await expect(page.locator('[data-popover]')).toBeVisible();
		await shootMotion(page, testInfo, '1440-36-text-comment-card-light');

		await page.setViewportSize({ width: 393, height: 852 });
		await page.emulateMedia({ colorScheme: 'light' });
		await openSuggestionDocument(page, request, text, []);
		const line = page.locator('.cm-line', { hasText: 'text comment is beside' });
		await expect(line).toBeVisible();
		await line.dblclick({ position: { x: 40, y: 8 } });
		await page.locator('.sel-comment').click();
		const sheet = page.locator('.drawer-panel[role="dialog"]');
		await expect(sheet.locator('textarea[placeholder="Comment…"]')).toBeVisible();
		await shootMotion(page, testInfo, '393-09-text-comment-composer-sheet-light');
	});
});
