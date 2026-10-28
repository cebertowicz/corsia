import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, sidebarThread, waitForWsConnected } from '../support/corsia';

/**
 * A status banner and the pane's tab strip never overlap.
 *
 * The strip lives in the shared header band and the banner sits just under it, but the pane starts below
 * the banner in flow, so a strip placed from the pane's top landed on the banner whenever one showed.
 */
test.describe('@flow:status-banner-tab-strip tab strip stays in the header band', () => {
	test('the Reconnecting banner sits below the open tab pill', async ({ page, request }, testInfo) => {
		test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'the expanded strip is a desktop surface');
		const title = `banner-strip-${Date.now()}`;
		await createSession(request, title);
		await page.setViewportSize({ width: 1440, height: 900 });
		await loginByUrl(page);
		await sidebarThread(page, title).click();
		await waitForWsConnected(page);
		await page.getByRole('button', { name: 'Open browser', exact: true }).click();
		const pill = page.locator('.canvas-header-strip .tab-wrap');
		await expect(pill).toBeVisible();
		const before = (await pill.boundingBox())!;

		await page.evaluate(async () => {
			const connection = await import('/src/lib/stores/connection.ts');
			connection.connectionState.set('disconnected');
		});
		const banner = page.locator('.connection-banner');
		await expect(banner).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'status-banner-tab-strip');

		const bannerBox = (await banner.boundingBox())!;
		const after = (await pill.boundingBox())!;
		// The pill keeps its place in the header band, clear of the banner underneath.
		expect(after.y + after.height).toBeLessThanOrEqual(bannerBox.y + 0.5);
		expect(Math.abs(after.y - before.y)).toBeLessThan(1);
		// The pane's content still starts below the banner, and the banner's own button stays reachable.
		const body = (await page.locator('.leaf-body').boundingBox())!;
		expect(body.y).toBeGreaterThanOrEqual(bannerBox.y + bannerBox.height - 0.5);
		const retry = page.getByRole('button', { name: 'Retry now' });
		const retryBox = (await retry.boundingBox())!;
		expect(await page.evaluate(([x, y]) => document.elementFromPoint(x, y)?.textContent?.trim(), [retryBox.x + retryBox.width / 2, retryBox.y + retryBox.height / 2])).toBe('Retry now');
	});
});
