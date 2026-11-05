import { expect } from '@playwright/test';
import { test } from '../support/test';
import { at, commentButton, composer, openThread, seedImage } from '../support/imageRegionComments';

// A growing comment draft must not trigger a resize-observer loop in its sheet.
test('@flow:doc-image-comment-resize-loop @surface:documents typing a phone image comment does not cause a resize loop', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'The phone viewport is set by the spec');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	test.setTimeout(90_000);
	await page.addInitScript(() => {
		const errors: string[] = [];
		(window as Window & { resizeLoopErrors: string[] }).resizeLoopErrors = errors;
		window.addEventListener('error', (event) => {
			if (event.message.includes('ResizeObserver loop')) errors.push(event.message);
		});
	});
	const seeded = await seedImage(page, request, 'image-comment-resize');
	await page.setViewportSize({ width: 375, height: 852 });
	await openThread(page, seeded.sessionTitle, 375);
	await commentButton(page).click();
	const spot = await at(page, 0.3, 0.4);
	await page.mouse.click(spot.x, spot.y);
	await expect(composer(page)).toBeVisible();
	await composer(page).pressSequentially('A comment with forty ordinary characters.', { delay: 25 });
	await page.waitForTimeout(500);
	const errors = await page.evaluate(() => (window as Window & { resizeLoopErrors: string[] }).resizeLoopErrors);
	expect(errors).toEqual([]);
});
