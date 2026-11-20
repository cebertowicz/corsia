import { expect } from '@playwright/test';
import { test } from '../support/test';
import { openComments } from '../support/commentDrawer';
import { openLoaded, seedLoaded } from '../support/loadedComments';
import { settled } from '../support/reviewFixtures';

test.use({ hasTouch: true, isMobile: true });

for (const width of [1440, 393]) {
	test(`loaded comment list clears the bar and dismisses at ${width}`, async ({ page, request }) => {
		test.setTimeout(240_000);
		const doc = await seedLoaded(request, `bar clearance ${width}`);
		await openLoaded(page, doc, { width, height: width === 393 ? 852 : 900, theme: 'light', touch: true, pane: 'open' });
		const drawer = page.locator('[data-doc-comments-drawer]');
		const show = async () => {
			await openComments(page);
			await expect(drawer).toBeVisible();
			await settled(page);
		};
		await show();
		const surface = width === 393 ? page.locator('.drawer-panel').filter({ has: drawer }) : page.locator('[data-popover]').filter({ has: drawer });
		await expect(surface).toBeVisible();
		const bar = await page.locator('.shell-header').boundingBox();
		const box = await surface.boundingBox();
		expect(bar).not.toBeNull(); expect(box).not.toBeNull();
		expect(box!.y, 'the list starts below the app bar').toBeGreaterThanOrEqual(bar!.y + bar!.height);
		if (width === 1440) {
			const entry = await page.locator('[data-comments-entry]').boundingBox();
			expect(entry).not.toBeNull();
			expect(box!.y, 'the list clears its entry by 8px').toBeGreaterThanOrEqual(entry!.y + entry!.height + 8);
		}
		await page.keyboard.press('Escape');
		await expect(drawer).not.toBeVisible();
		await show();
		const canvas = await page.locator('.doc').boundingBox();
		expect(canvas).not.toBeNull();
		if (width === 393) await page.touchscreen.tap(canvas!.x + canvas!.width / 2, canvas!.y + 4);
		else await page.mouse.click(canvas!.x + 2, canvas!.y + canvas!.height - 2);
		await expect(drawer).not.toBeVisible();
	});
}
