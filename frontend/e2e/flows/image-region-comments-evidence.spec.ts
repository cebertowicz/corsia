import fs from 'node:fs';
import path from 'node:path';
import { expect, test, type APIRequestContext, type Locator, type Page, type TestInfo } from '@playwright/test';
import { openSuggestionDocument } from '../support/docSuggestions';
import {
	apiComment, appScreenshotPng, asAgent, at, commentButton, composer, drag, drawer, drawerList, expectTransition, fitButton, frame,
	inside, newRevision, openThread, overlap, pill, pressFrame, record, recordScroll, rest, seedImage, sheet, shoot, showImage, still,
	tag, toast, touchDrag, viewer, type Region, type Seeded,
} from '../support/imageRegionComments';

// Review evidence for the composed surface: recordings of each transition, held presses, failure states and a busy
// real screen. Each recording also reads the changed element itself, so a frame set cannot pass on another animation.

const desktopOnly = (testInfo: TestInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
};
const layout = (page: Page) => page.locator('.image-comments .layout');
const box = (page: Page, id: string) => page.locator(`.image-viewer [data-region-box="${id}"]:not([inert])`);
const opacity = (page: Page, id: string) => () => box(page, id).evaluate((el) => Number(getComputedStyle(el).opacity).toFixed(2));
/** The image's cell, which gives up the drawer's room below or beside it. */
const viewerSize = (page: Page) => () => page.locator('.image-comments .viewer-cell').evaluate((el) => {
	const r = el.getBoundingClientRect();
	return `${Math.round(r.width)}x${Math.round(r.height)}`;
});
/** The image cell's size and how far the drawer's list is scrolled, so opening shows whether the list moves with the drawer. */
const openingDrawer = (page: Page) => async () => `${await viewerSize(page)()} list ${await page.evaluate(() => document.querySelector('.canvas-comment-drawer .drawer-comments')?.scrollTop ?? '-')}`;
/**
 * Every tag and mark of a pin being moved: what it is, where, its opacity, and its dashed layer's opacity
 * (`old` is the place it is leaving; `inert` ones are fading out). One reading per frame shows the fades.
 */
const movingPin = (page: Page) => () => viewer(page).evaluate((root) => [...root.querySelectorAll<HTMLElement>('[data-region-tag], [data-region-box], [data-region-dot], [data-region-from]')]
	.map((el) => {
		const kind = el.dataset.regionTag ? 'tag' : el.dataset.regionFrom ? 'old' : el.dataset.regionDot ? 'dot' : 'box';
		const dashed = el.dataset.regionTag ? getComputedStyle(el.querySelector('.disc')!, '::before').opacity : el.dataset.regionDot || (el.dataset.regionFrom && !el.style.width) ? getComputedStyle(el).outlineColor : getComputedStyle(el, '::before').opacity;
		const r = el.getBoundingClientRect();
		return `${kind}${el.inert ? '(inert)' : ''}@${Math.round(r.x)},${Math.round(r.y)} o=${Number(getComputedStyle(el).opacity).toFixed(2)} dashed=${dashed}`;
	}).join(' | '));
const background = (el: Element) => getComputedStyle(el).backgroundColor;
const discPress = (el: Element) => { const disc = getComputedStyle(el.querySelector('.disc')!); return `${disc.scale} ${disc.backgroundColor}`; };

/**
 * Twelve regions on a busy screen: boxes, single points, and four points 40 px apart on screen, then a text comment.
 * Seeded one at a time, so note N is always tag N.
 */
async function seedDense(page: Page, request: APIRequestContext, seeded: Seeded): Promise<string[]> {
	const step = 40 / (await frame(page)).width;
	const regions: Region[] = [
		{ x: .08, y: .12, w: 0, h: 0 }, { x: .22, y: .16, w: .12, h: .1 }, { x: .44, y: .1, w: 0, h: 0 }, { x: .68, y: .14, w: .2, h: .12 },
		{ x: .9, y: .1, w: .08, h: .15 }, { x: .1, y: .42, w: .16, h: .18 }, { x: .37, y: .46, w: 0, h: 0 }, { x: .37 + step, y: .46, w: 0, h: 0 },
		{ x: .37 + 2 * step, y: .46, w: 0, h: 0 }, { x: .37 + step, y: .46 + step * 1.6, w: 0, h: 0 }, { x: .62, y: .5, w: .14, h: .18 },
		{ x: .78, y: .76, w: .18, h: .17 },
	];
	const ids: string[] = [];
	for (const [index, region] of regions.entries()) ids.push(await apiComment(request, seeded.artifactId, `Dense review note ${index + 1}`, region));
	await apiComment(request, seeded.artifactId, 'Text comment beside the image comments');
	for (const index of [2, 7]) asAgent(seeded, ids[index]);
	return ids;
}

/** Every tag is on top at its own number, so each can be reached by pointer, however close the points are. */
async function expectEveryTagReachable(page: Page, count: number) {
	for (let number = 1; number <= count; number++) {
		const disc = (await tag(page, number).locator('.disc').boundingBox())!;
		const hit = await page.evaluate(({ x, y }) => (document.elementFromPoint(x, y)?.closest('[data-region-tag]') as HTMLElement | null)?.dataset.regionTag ?? null,
			{ x: disc.x + disc.width / 2, y: disc.y + disc.height / 2 });
		expect(hit, `tag ${number} is on top at its number`).toBe(String(number));
	}
}

/**
 * Waits until the layout's own transitions finish and `target` neither moves nor scrolls between two reads 150 ms apart,
 * so a sliding drawer is measured where it lands, even when a busy machine stalls frames.
 */
async function settled(page: Page, target: Locator) {
	await layout(page).evaluate((el) => Promise.all(el.getAnimations().map((animation) => animation.finished)));
	let last = '';
	await expect.poll(async () => {
		const now = JSON.stringify([await target.boundingBox(), await target.evaluate((el) => el.scrollTop)]);
		const same = now === last;
		last = now;
		return same;
	}, { intervals: [150] }).toBe(true);
}
/** The image request answers 404, so the viewer shows why it could not load. */
async function failImage(page: Page, seeded: Seeded) {
	const name = encodeURIComponent(path.basename(seeded.imagePath));
	await page.route('**/api/files/content**', (route) => route.request().resourceType() === 'image' && route.request().url().includes(name)
		? route.fulfill({ status: 404, body: 'not found' }) : route.continue());
}
/** Posting a comment fails once with a server error. */
async function failNextPost(page: Page, seeded: Seeded) {
	await page.route(`**/api/artifacts/${seeded.artifactId}/comments**`, (route) => route.request().method() === 'POST'
		? route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"unavailable"}' }) : route.continue());
}
/** Holds the next pin move until released, then fails it with a server error. */
/** Holds the next pin move until released, then fails it or lets it through. */
async function holdMove(page: Page, fail = true): Promise<() => Promise<void>> {
	let release: () => void = () => {};
	const held = new Promise<void>((resolve) => { release = resolve; });
	await page.route('**/api/comments/**', async (route) => {
		if (route.request().method() !== 'PATCH') return route.continue();
		await held;
		if (!fail) return route.continue();
		await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"unavailable"}' });
	});
	return async () => release();
}

test.describe('@flow:image-region-comments @surface:documents image comment review evidence', () => {
	test('a busy screen: dense tags, the drawer linked to the image, and every transition (1440)', async ({ page, request }, testInfo) => {
		desktopOnly(testInfo);
		test.setTimeout(240_000);
		const seeded = await seedImage(page, request, 'region-dense', 1200, await appScreenshotPng(page));
		try {
			await page.setViewportSize({ width: 1440, height: 900 });
			await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'no-preference' });
			await openThread(page, seeded.sessionTitle, 1440);
			const ids = await seedDense(page, request, seeded);
			await page.reload();
			await showImage(page, seeded.sessionTitle);
			await expect(pill(page)).toHaveAttribute('aria-label', /^13 open threads(, |$)/);
			await rest(page);
			await expectEveryTagReachable(page, 12);
			await shoot(page, testInfo, '1440-15-dense-rest', true);
			await tag(page, 7).hover();
			await shoot(page, testInfo, '1440-16-dense-hover', true);

			// Picking a tag by keyboard lands in its card's reply field.
			await tag(page, 8).focus();
			await page.keyboard.press('Enter');
			await expect(tag(page, 8)).toHaveAttribute('aria-pressed', 'true');
			await expect(page.locator('[data-popover] .ct-input')).toBeFocused();
			// The picked agent tag rises over its close neighbours, so its name is never covered.
			const name = (await tag(page, 8).locator('.name').boundingBox())!;
			expect(await page.evaluate(({ x, y }) => (document.elementFromPoint(x, y)?.closest('[data-region-tag]') as HTMLElement | null)?.dataset.regionTag,
				{ x: name.x + name.width / 2, y: name.y + name.height / 2 })).toBe('8');
			await shoot(page, testInfo, '1440-17-dense-selected', true);
			await page.keyboard.press('Escape');

			// The drawer: at least three whole cards with thirteen comments, the text comment among them.
			await pill(page).click();
			await expect(drawer(page)).toBeVisible();
			await settled(page, drawerList(page));
			await expect(drawerList(page).locator('.ct')).toHaveCount(13);
			const listBox = (await drawerList(page).boundingBox())!;
			const cardBoxes = await drawerList(page).locator('.ct').evaluateAll((cards) => cards.map((card) => {
				const r = card.getBoundingClientRect();
				return { x: r.x, y: r.y, width: r.width, height: r.height };
			}));
			expect(cardBoxes.filter((card) => inside(card, listBox)).length).toBeGreaterThanOrEqual(3);
			await shoot(page, testInfo, '1440-18-drawer-13', true);
			const textCard = drawerList(page).locator('.ct').filter({ hasText: 'Text comment beside the image comments' });
			await textCard.scrollIntoViewIfNeeded();
			// Scrolled to the end, the list's integer scroll range leaves the card's fractional bottom edge up to a pixel past it.
			await expect(textCard).toBeInViewport({ ratio: 0.98 });
			await shoot(page, testInfo, '1440-18b-drawer-text-comment', true);
			await drawerList(page).evaluate((el) => { el.scrollTop = 0; });

			// Linked: hovering a card fades in its box; it never scrolls anything.
			const card2 = drawerList(page).locator(`[data-comment-id="${ids[1]}"]`);
			const listTop = await drawerList(page).evaluate((el) => el.scrollTop);
			expectTransition(await record(page, testInfo, '1440-37-link-card-hover-box', layout(page), () => card2.hover(), opacity(page, ids[1])), 'hovered card\'s box');
			expect(await drawerList(page).evaluate((el) => el.scrollTop)).toBe(listTop);
			await drawer(page).locator('.drawer-heading').hover();

			// Linked: selecting a card glides the zoomed image to its tag.
			await viewer(page).getByRole('button', { name: '100%', exact: true }).click();
			const viewport = viewer(page).locator('.viewport');
			await viewport.evaluate((el) => { el.scrollLeft = 0; el.scrollTop = 0; });
			const card12 = drawerList(page).locator(`[data-comment-id="${ids[11]}"]`);
			await card12.scrollIntoViewIfNeeded();
			const glide = await recordScroll(page, testInfo, '1440-38-link-card-scrolls-image', layout(page), viewport, () => card12.locator('.ct').click());
			expect(glide.length, `image scroll positions ${JSON.stringify(glide)}`).toBeGreaterThanOrEqual(3);
			expect(inside((await tag(page, 12).boundingBox())!, (await viewport.boundingBox())!)).toBe(true);
			await fitButton(page).click();

			// Linked: picking a tag keeps the drawer, scrolls it to the card and marks it.
			await drawerList(page).evaluate((el) => { el.scrollTop = 0; });
			const toCard = await recordScroll(page, testInfo, '1440-39-link-tag-scrolls-drawer', layout(page), drawerList(page), () => tag(page, 12).click());
			expect(toCard.length, `drawer scroll positions ${JSON.stringify(toCard)}`).toBeGreaterThanOrEqual(3);
			const marked = drawerList(page).locator(`[data-comment-id="${ids[11]}"] .ct`);
			await expect(marked).toHaveClass(/focused/);
			expect(inside((await marked.boundingBox())!, (await drawerList(page).boundingBox())!)).toBe(true);

			// The drawer slides shut and open, and the image refits with it.
			await rest(page);
			const pillCentre = async () => { const b = (await pill(page).boundingBox())!; return { x: b.x + b.width / 2, y: b.y + b.height / 2 }; };
			expectTransition(await record(page, testInfo, '1440-35-drawer-close', layout(page), () => page.keyboard.press('Escape'), viewerSize(page), { frames: 14 }), 'image cell as the drawer closes');
			await expect(drawer(page)).toHaveCount(0);
			const opening = await pillCentre();
			// Tag 12 is selected: the drawer slides in already at its card, so the list never scrolls while it opens.
			const opened = await record(page, testInfo, '1440-34-drawer-open', layout(page), () => page.mouse.click(opening.x, opening.y), openingDrawer(page), { frames: 14 });
			expectTransition(opened, 'image cell as the drawer opens');
			const listScroll = new Set(opened.map((value) => String(value).split(' list ')[1]).filter((value) => value !== '-'));
			expect(listScroll.size, `the list holds still while the drawer opens: ${JSON.stringify(opened)}`).toBe(1);
			await page.keyboard.press('Escape');
			await expect(drawer(page)).toHaveCount(0);

			// A box fades in on hover and out on leaving.
			await rest(page);
			const disc2 = (await tag(page, 2).locator('.disc').boundingBox())!;
			expectTransition(await record(page, testInfo, '1440-31-box-fade-in', viewer(page), () => page.mouse.move(disc2.x + disc2.width / 2, disc2.y + disc2.height / 2), opacity(page, ids[1])), 'box fading in');
			expectTransition(await record(page, testInfo, '1440-31b-box-fade-out', viewer(page), () => rest(page), opacity(page, ids[1])), 'box fading out');

			// Box mode's colour change eases on the shared bar control.
			const bar = viewer(page).locator('.bar');
			const comment = async () => { const b = (await commentButton(page).boundingBox())!; await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2); };
			const commentColour = () => commentButton(page).evaluate(background);
			expectTransition(await record(page, testInfo, '1440-24-box-mode-on', bar, comment, commentColour), 'Comment turning on');
			await rest(page);
			expectTransition(await record(page, testInfo, '1440-25-box-mode-off', bar, comment, commentColour), 'Comment turning off');

			// Held presses tint at once.
			await rest(page);
			await pressFrame(page, commentButton(page), testInfo, '1440-23-press-comment', background);
			await pressFrame(page, pill(page), testInfo, '1440-33-press-pill', background);
			await pressFrame(page, fitButton(page), testInfo, '1440-33b-press-fit', background);
			await pressFrame(page, tag(page, 2), testInfo, '1440-29-press-tag', discPress);

			// Reduced motion: the drawer arrives without the slide.
			await page.emulateMedia({ reducedMotion: 'reduce' });
			const reduced = await record(page, testInfo, '1440-34r-drawer-open-reduced', layout(page), () => pill(page).click(), viewerSize(page), { frames: 6 });
			expect(new Set(reduced).size, `reduced-motion sizes ${JSON.stringify(reduced)}`).toBeLessThanOrEqual(2);
		} finally {
			fs.rmSync(seeded.imagePath, { force: true });
		}
	});

	test('failure states and moving a pin (1440)', async ({ page, request }, testInfo) => {
		desktopOnly(testInfo);
		test.setTimeout(180_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.emulateMedia({ colorScheme: 'dark' });
		const seeded = await seedImage(page, request, 'region-failures', 1200);
		try {
			await openThread(page, seeded.sessionTitle, 1440);

			// A comment that fails to post keeps its text and says so; sending again posts it.
			await failNextPost(page, seeded);
			await drag(page, await at(page, .2, .25), await at(page, .45, .5));
			await composer(page).fill('The header is too tall here');
			await page.keyboard.press('ControlOrMeta+Enter');
			await expect(toast(page, "Couldn't post the comment")).toBeVisible();
			await expect(composer(page)).toHaveValue('The header is too tall here');
			await shoot(page, testInfo, '1440-40-post-failed', true);
			await page.unroute(`**/api/artifacts/${seeded.artifactId}/comments**`);
			await composer(page).focus();
			await page.keyboard.press('ControlOrMeta+Enter');
			await expect(composer(page)).toHaveCount(0);
			await expect(tag(page, 1)).toBeVisible();
			await page.keyboard.press('Escape');

			// Moving a stale pin: the new place shows pending with its number, the old one dashed, until the move lands or fails.
			newRevision(seeded);
			await page.reload();
			await showImage(page, seeded.sessionTitle);
			await tag(page, 1).click();
			await page.locator('[data-popover]').getByRole('button', { name: 'Move pin' }).click();
			await expect(commentButton(page)).toContainText('Move pin');
			await still(page, testInfo, '1440-20b-move-pin-start');
			const release = await holdMove(page);
			const target = await at(page, .62, .64);
			// The pin fades in, dashed, at its new place, its old tag fades out, and its old box turns dashed and dims.
			expectTransition(await record(page, testInfo, '1440-21m-move-start-to-pending', viewer(page), () => page.mouse.click(target.x, target.y), movingPin(page)), 'moved pin going pending');
			await expect(commentButton(page)).toContainText('Moving pin…');
			await expect(tag(page, 1)).toHaveClass(/pending/);
			await expect(page.locator('.image-viewer [data-region-from]')).toHaveCount(1);
			await shoot(page, testInfo, '1440-21-move-pin-pending', true);
			// Failing: the pending pin fades out and the old one comes back solid, in place.
			// The toast lasts three seconds and the filming takes longer, so wait for it while the film runs.
			const moveFailed = toast(page, "Couldn't move the pin").waitFor(); moveFailed.catch(() => {});
			expectTransition(await record(page, testInfo, '1440-21n-pending-to-failed', viewer(page), release, movingPin(page)), 'failed move reverting');
			await moveFailed;
			await expect(tag(page, 1)).not.toHaveClass(/pending/);
			await expect(tag(page, 1)).toHaveClass(/stale/);
			await expect(page.locator('.image-viewer [data-region-from]')).toHaveCount(0);
			await shoot(page, testInfo, '1440-21b-move-pin-failed', true);
			await page.unroute('**/api/comments/**');

			// Trying again lands it: the pending pin settles from dashed to solid where it is, and the old place fades out.
			const land = await holdMove(page, false);
			await tag(page, 1).click();
			await page.locator('[data-popover]').getByRole('button', { name: 'Move pin' }).click();
			await page.mouse.click(target.x, target.y);
			await expect(tag(page, 1)).toHaveClass(/pending/);
			await rest(page);
			// Filmed from a still pending pin: the fades that started it have finished.
			await expect(viewer(page).locator('[data-region-tag][inert], [data-region-from][inert]')).toHaveCount(0);
			await expect.poll(() => tag(page, 1).evaluate((el) => getComputedStyle(el).opacity)).toBe('1');
			expectTransition(await record(page, testInfo, '1440-22m-pending-to-settled', viewer(page), land, movingPin(page)), 'moved pin settling');
			await page.unroute('**/api/comments/**');
			await expect(commentButton(page)).not.toContainText('Moving pin…');
			await expect(tag(page, 1)).not.toHaveClass(/stale/);
			await shoot(page, testInfo, '1440-22-move-pin-settled', true);

			// An image that cannot load says why; nothing can be drawn on it.
			await failImage(page, seeded);
			await page.reload();
			await page.getByText(seeded.sessionTitle).first().click();
			await expect(viewer(page).locator('.load-error')).toBeVisible();
			await expect(viewer(page).getByRole('button', { name: 'Try again' })).toBeVisible();
			await expect(commentButton(page)).toHaveCount(0);
			await shoot(page, testInfo, '1440-41-image-failed', true);
		} finally {
			fs.rmSync(seeded.imagePath, { force: true });
		}
	});

	test('text comment neighbour card and phone composer', async ({ page, request }, testInfo) => {
		desktopOnly(testInfo);
		test.setTimeout(120_000);
		const text = '# Review target\n\nThe text comment is beside the image-comment surface.';
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const desktop = await openSuggestionDocument(page, request, text, [{ quote: 'text comment is beside', body: 'Text review note beside an image comment.' }]);
		await page.locator(`.cm-comment-marker[data-comment-ids*="${desktop.ids[0]}"]`).click();
		await expect(page.locator('[data-popover]')).toBeVisible();
		await still(page, testInfo, '1440-36-text-comment-card-light');

		await page.setViewportSize({ width: 393, height: 852 });
		await openSuggestionDocument(page, request, text, []);
		const line = page.locator('.cm-line', { hasText: 'text comment is beside' });
		await expect(line).toBeVisible();
		await line.dblclick({ position: { x: 40, y: 8 } });
		await page.locator('.sel-comment').click();
		await expect(sheet(page).locator('textarea[placeholder="Comment…"]')).toBeVisible();
		await still(page, testInfo, '393-09-text-comment-composer-sheet-light');
	});
});

test.describe('@flow:image-region-comments @surface:documents image comment review evidence, by touch', () => {
	test.use({ hasTouch: true });

	test('a busy screen, moving a pin and failure states on a phone (393)', async ({ page, request }, testInfo) => {
		desktopOnly(testInfo);
		test.setTimeout(240_000);
		const seeded = await seedImage(page, request, 'region-dense-393', null, await appScreenshotPng(page));
		try {
			await page.setViewportSize({ width: 393, height: 852 });
			await page.emulateMedia({ colorScheme: 'dark' });
			await openThread(page, seeded.sessionTitle, 393);
			await seedDense(page, request, seeded);
			await page.reload();
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await showImage(page, seeded.sessionTitle);
			await expect(pill(page)).toHaveAttribute('aria-label', /^13 open threads(, |$)/);
			await expectEveryTagReachable(page, 12);
			await shoot(page, testInfo, '393-06-dense-rest', true);
			// The disc, since at this size a close neighbour's disc may sit over the hidden name beside it.
			await tag(page, 8).locator('.disc').tap();
			await expect(sheet(page).locator('.ct')).toBeVisible();
			expect(overlap((await tag(page, 8).boundingBox())!, (await sheet(page).boundingBox())!)).toBe(false);
			await shoot(page, testInfo, '393-07-dense-selected', true);
			await page.keyboard.press('Escape');
			await pill(page).tap();
			await expect(drawer(page).locator('.ct')).toHaveCount(13);
			await shoot(page, testInfo, '393-08-dense-drawer', true);
			await page.keyboard.press('Escape');

			// Moving a stale pin with one finger: pending, then failed.
			newRevision(seeded);
			await page.reload();
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await showImage(page, seeded.sessionTitle);
			await tag(page, 1).tap();
			await sheet(page).getByRole('button', { name: 'Move pin' }).tap();
			await expect(sheet(page)).toHaveCount(0);
			const release = await holdMove(page);
			// Clear of every tag, since a tag keeps its own press in box mode. A phone frame is slow to take, so its clock runs slower.
			const spot = await at(page, .35, .85);
			expectTransition(await record(page, testInfo, '393-10m-move-start-to-pending', viewer(page), () => touchDrag(page, spot, spot), movingPin(page), { rate: 0.03 }), 'moved pin going pending (393)');
			await expect(commentButton(page)).toContainText('Moving pin…');
			await expect(tag(page, 1)).toHaveClass(/pending/);
			await shoot(page, testInfo, '393-10-move-pin-pending', true);
			const moveFailed = toast(page, "Couldn't move the pin").waitFor(); moveFailed.catch(() => {});
			expectTransition(await record(page, testInfo, '393-11m-pending-to-failed', viewer(page), release, movingPin(page), { rate: 0.03 }), 'failed move reverting (393)');
			await moveFailed;
			await expect(tag(page, 1)).not.toHaveClass(/pending/);
			await shoot(page, testInfo, '393-11-move-pin-failed', true);
			await page.unroute('**/api/comments/**');

			// A comment that fails to post keeps its text in the sheet.
			await failNextPost(page, seeded);
			await commentButton(page).tap();
			await touchDrag(page, await at(page, .15, .75), await at(page, .45, .95));
			await sheet(page).locator('textarea').fill('Too much padding on phones');
			await sheet(page).getByRole('button', { name: 'Comment', exact: true }).tap();
			await expect(toast(page, "Couldn't post the comment")).toBeVisible();
			await expect(sheet(page).locator('textarea')).toHaveValue('Too much padding on phones');
			await shoot(page, testInfo, '393-12-post-failed', true);
			await page.unroute(`**/api/artifacts/${seeded.artifactId}/comments**`);
			await page.keyboard.press('Escape');

			// An image that cannot load says why.
			await failImage(page, seeded);
			await page.reload();
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await page.getByText(seeded.sessionTitle).first().click();
			await expect(viewer(page).locator('.load-error')).toBeVisible();
			await shoot(page, testInfo, '393-13-image-failed', true);
		} finally {
			fs.rmSync(seeded.imagePath, { force: true });
		}
	});
});
