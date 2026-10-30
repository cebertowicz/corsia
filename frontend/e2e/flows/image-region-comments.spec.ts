import fs from 'node:fs';
import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';
import { imageBarSend, sendByKeyboard } from '../support/heldComments';
import {
	at, commentButton, composer, drag, drawer, frame, isolatedDb, newRevision, openThread, overlap, pill, postComment, rest, seedImage,
	sheet, shoot, showImage, tag, tagNumbers, touchDrag, viewer,
} from '../support/imageRegionComments';

/** The card is wholly on screen and on top at each corner, so no ancestor clips or covers it. */
async function expectUnclipped(page: Page, card: Locator) {
	const box = (await card.boundingBox())!;
	const view = page.viewportSize()!;
	expect(box.x >= 0 && box.y >= 0 && box.x + box.width <= view.width && box.y + box.height <= view.height, JSON.stringify(box)).toBe(true);
	const corners = [[box.x + 3, box.y + 3], [box.x + box.width - 3, box.y + 3], [box.x + 3, box.y + box.height - 3], [box.x + box.width - 3, box.y + box.height - 3]];
	for (const [x, y] of corners) {
		expect(await card.evaluate((el, point) => el.contains(document.elementFromPoint(point.x, point.y)), { x, y }), `card corner ${x},${y}`).toBe(true);
	}
}

test.describe('@flow:image-region-comments @surface:documents comments on parts of an image', () => {
	test('desktop: draw, number, reply, resolve, list, deliver, agent tag and stale pins (1440)', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(180_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.emulateMedia({ colorScheme: 'dark' });
		const seeded = await seedImage(page, request, 'region-1440');
		try {
			await openThread(page, seeded.sessionTitle, 1440);
			await expect(pill(page)).toHaveAttribute('aria-label', '0 open threads');
			await expect(commentButton(page)).toBeVisible();
			await shoot(page, testInfo, '1440-01-open', true);

			// A mouse drag draws a box anywhere on the image; the composer opens beside it.
			await drag(page, await at(page, 0.2, 0.25), await at(page, 0.45, 0.5));
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
			expect(overlap((await page.locator('.image-viewer [data-region-box]:not([inert])').boundingBox())!, cardBox)).toBe(false);
			await expectUnclipped(page, card);
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
			for (const regionTag of await page.locator('.image-viewer [data-region-tag]:not([inert])').all()) {
				expect(overlap((await regionTag.boundingBox())!, neighbouringCardBox)).toBe(false);
			}
			await page.keyboard.press('Escape');
			await shoot(page, testInfo, '1440-04-tags-faint', true);
			await tag(page, 1).hover();
			await expect(tag(page, 1)).not.toHaveClass(/faint/);
			await expect(page.locator('.image-viewer [data-region-box]:not([inert])')).toHaveCount(1);
			await shoot(page, testInfo, '1440-05-tag-hover');

			// Reply on the point.
			await tag(page, 2).click();
			const pointCard = page.locator('.ct').filter({ hasText: 'This icon reads as disabled' });
			await expect(pointCard).toBeVisible();
			await pointCard.getByPlaceholder('Reply…').fill('Agreed, it needs more contrast');
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
			await expect(pill(page)).toHaveAttribute('aria-label', /^2 open threads(, |$)/);
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
			const filter = drawer.getByRole('button', { name: 'Filter comments: All', exact: true });
			await filter.click();
			await expect(filter).toHaveAttribute('aria-expanded', 'true');
			await expect(drawer.getByRole('menu', { name: 'Filter comments', exact: true })).toBeVisible();
			await drawer.getByRole('menuitemradio', { name: 'Resolved', exact: true }).click();
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

			// The viewer bar sends the held image batch by keyboard; focus returns to its durable Comment control.
			await expect(imageBarSend(page)).toHaveText(/^\s*Send \d+ comments?\s*$/);
			await sendByKeyboard(page, imageBarSend(page), undefined, commentButton(page));
			// All three regions reach one task carrying each region and one marked-up copy.
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
			// The copy is drawn after the delivery commits, so it lands a moment after the batch reads delivered.
			await expect.poll(() => fs.existsSync(marked![1]), { timeout: 15_000 }).toBe(true);
			fs.copyFileSync(marked![1], testInfo.outputPath('1440-11-delivered-marked-up-image.png'));

			// An agent's comment carries its name beside the number.
			const tok = encodeURIComponent(TEST_TOKEN);
			const agentComment = await request.post(`/api/artifacts/${seeded.artifactId}/comments?token=${tok}`, {
				data: { body: 'The call-to-action is clipped at this width.', region: { x: 0.55, y: 0.6, w: 0.3, h: 0.25 }, delivery_policy: 'immediate' },
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
			newRevision(seeded);
			await page.reload();
			await showImage(page, seeded.sessionTitle);
			await expect(tag(page, 2)).toHaveClass(/stale/);
			await expect(tag(page, 4)).toHaveClass(/stale/);
			await expect(tag(page, 2)).toHaveAttribute('aria-label', /image changed since this comment/);
			// The dashed edge is the disc's own layer, shown over a cleared solid border.
			await expect.poll(() => tag(page, 2).locator('.disc').evaluate((el) => {
				const edge = getComputedStyle(el, '::before');
				return `${edge.borderTopStyle} ${edge.opacity} ${getComputedStyle(el).borderTopColor}`;
			})).toBe('dashed 1 rgba(0, 0, 0, 0)');
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
			await expect(pill(page)).toHaveAttribute('aria-label', '0 open threads');
			// The fit settles after the canvas widens; draw on the settled image.
			let last = 0;
			await expect.poll(async () => { const w = (await frame(page)).width; const settled = w === last; last = w; return settled; }).toBe(true);
			await drag(page, await at(page, 0.15, 0.3), await at(page, 0.35, 0.55));
			await postComment(page, 'The header is too tall here');
			const card = page.locator('.ct').filter({ hasText: 'The header is too tall here' });
			await expect(card).toBeVisible();
			const cardBox = (await card.boundingBox())!;
			const box = (await page.locator('.image-viewer [data-region-box]:not([inert])').boundingBox())!;
			expect(overlap(box, cardBox)).toBe(false);
			expect(overlap((await tag(page, 1).boundingBox())!, cardBox)).toBe(false);
			// Beside: the card shares the box's rows instead of dropping below it.
			expect(cardBox.y).toBeLessThan(box.y + box.height);
			// The drawer's sliding track clips the layout; the card is fixed-position and escapes that clip.
			await expectUnclipped(page, card);
			await shoot(page, testInfo, '1440-14-wide-card-beside-box', true);

			// The viewer bar's one Send is keyboard-operated; focus returns to Comment after it disappears.
			await page.keyboard.press('Escape');
			await expect(imageBarSend(page)).toHaveText(/^\s*Send \d+ comments?\s*$/);
			await sendByKeyboard(page, imageBarSend(page), undefined, commentButton(page));
			await shoot(page, testInfo, '1440-15-bar-sent', true);
		} finally {
			fs.rmSync(seeded.imagePath, { force: true });
		}
	});

});

test.describe('@flow:image-region-comments @surface:documents comments on parts of an image, by touch', () => {
	test.use({ hasTouch: true });

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

			await commentButton(page).tap();
			await expect(commentButton(page)).toHaveAttribute('aria-pressed', 'true');
			await shoot(page, testInfo, '393-02-box-mode', true);
			await touchDrag(page, await at(page, 0.2, 0.3), await at(page, 0.6, 0.6));
			await expect(sheet(page)).toBeVisible();
			await expect(sheet(page).locator('textarea')).toBeVisible();
			await sheet(page).locator('textarea').fill('Too much padding on phones');
			await shoot(page, testInfo, '393-03-sheet-composer', true);
			await sheet(page).getByRole('button', { name: 'Comment', exact: true }).tap();
			await expect(sheet(page).locator('.ct-num')).toHaveText('1');
			// Box mode ended after one comment.
			await expect(commentButton(page)).toHaveAttribute('aria-pressed', 'false');
			await shoot(page, testInfo, '393-04-sheet-card', true);
			await page.keyboard.press('Escape');
			await expect(sheet(page)).toHaveCount(0);

			// Outside box mode one finger pans again: nothing is drawn.
			await touchDrag(page, await at(page, 0.7, 0.7), await at(page, 0.5, 0.5));
			await expect(composer(page)).toHaveCount(0);

			// The tag's touch target is 44 px around its number.
			const disc = (await tag(page, 1).locator('.disc').boundingBox())!;
			const centre = { x: disc.x + disc.width / 2, y: disc.y + disc.height / 2 };
			for (const [dx, dy] of [[-20, 0], [20, 0], [0, -20], [0, 20]]) {
				expect(await page.evaluate(({ x, y }) => !!document.elementFromPoint(x, y)?.closest('[data-region-tag="1"]'), { x: centre.x + dx, y: centre.y + dy })).toBe(true);
			}
			await tag(page, 1).tap();
			await expect(sheet(page)).toBeVisible();
			await expect(sheet(page).getByText('Too much padding on phones')).toBeVisible();
			// The sheet leaves the picked tag in view.
			expect(overlap((await tag(page, 1).boundingBox())!, (await sheet(page).boundingBox())!)).toBe(false);
			await shoot(page, testInfo, '393-05-tag-opens-sheet', true);

			// The viewer bar's one Send is keyboard-operated; focus returns to Comment after it disappears.
			// (Sending delivers to the thread's owner, so only against the isolated mock state.)
			if (process.env.CORSIA_E2E_MOCK === '1') {
				await page.keyboard.press('Escape');
				await expect(sheet(page)).toHaveCount(0);
				await expect(imageBarSend(page)).toHaveText(/^\s*Send \d+ comments?\s*$/);
				await sendByKeyboard(page, imageBarSend(page), undefined, commentButton(page));
				await shoot(page, testInfo, '393-06-bar-sent', true);
			}
		} finally {
			fs.rmSync(seeded.imagePath, { force: true });
		}
	});
});
