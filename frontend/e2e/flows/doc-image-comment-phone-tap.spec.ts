import { test } from '../support/test';
import { expect } from '@playwright/test';
import { at, commentButton, composer, openThread, seedImage, sheet, shoot, tag } from '../support/imageRegionComments';

test.use({ hasTouch: true });

// A phone reader presses Comment and taps the image: the comment box opens and stays open for typing and posting.
// The tap's own click arrives after the box opens, and must not read as a tap outside it.
test('@flow:doc-image-comment-phone-tap @surface:documents a tap in comment mode on a phone keeps the comment box (393)', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Size and touch are set by the spec itself');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	test.setTimeout(90_000);
	const seeded = await seedImage(page, request, 'region-tap-393');
	await page.setViewportSize({ width: 393, height: 852 });
	await openThread(page, seeded.sessionTitle, 393);

	await commentButton(page).tap();
	const spot = await at(page, 0.3, 0.4);
	await page.touchscreen.tap(spot.x, spot.y);
	await expect(composer(page), 'the comment box opens after the tap').toBeVisible();
	await page.waitForTimeout(800);
	await expect(composer(page), 'and is still there once the tap has settled').toBeVisible();
	// A tap that really lands outside the sheet still puts it away.
	await page.touchscreen.tap(196, 12);
	await expect(composer(page)).toHaveCount(0);
	await commentButton(page).tap();
	await page.touchscreen.tap(spot.x, spot.y);
	await expect(composer(page)).toBeVisible();
	await composer(page).fill('Typed on a phone');
	await shoot(page, testInfo, '393-tap-typed');
	await sheet(page).getByRole('button', { name: 'Comment', exact: true }).tap();
	await expect(composer(page)).toHaveCount(0);
	await expect(tag(page, 1)).toBeVisible();
	await shoot(page, testInfo, '393-tap-posted');
});
