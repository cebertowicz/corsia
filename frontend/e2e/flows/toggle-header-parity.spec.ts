import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { loginByUrl, openSettings, openSettingsSection } from '../support/corsia';
import { settled } from '../support/reviewFixtures';

for (const width of [1440, 393]) for (const theme of ['light', 'dark'] as const) {
	test(`shared Toggle keeps its normal settings size and behavior (${width} ${theme})`, async ({ page }) => {
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
		await loginByUrl(page, { waitForComposer: false });
		await openSettings(page); await openSettingsSection(page, 'System');
		const toggle = page.getByRole('button', { name: 'Focus mode', exact: true });
		await expect(toggle).toBeVisible(); await expect(toggle).toBeEnabled();
		await settled(page);
		const box = (await toggle.boundingBox())!;
		expect({ width: box.width, height: box.height }).toEqual({ width: 34, height: 19 });
		const tracks = await page.locator('.settings-view .tg').evaluateAll((buttons) => buttons.map((button) => {
			const box = button.getBoundingClientRect(), track = getComputedStyle(button, '::before');
			return { width: box.width, height: box.height, trackWidth: parseFloat(track.width), trackHeight: parseFloat(track.height) };
		}));
		for (const item of tracks) { expect(item.width).toBe(item.trackWidth); expect(item.height).toBe(item.trackHeight); }
		const before = await toggle.getAttribute('aria-pressed');
		await toggle.click(); await expect(toggle).toHaveAttribute('aria-pressed', before === 'true' ? 'false' : 'true');
		await expect(toggle).toBeEnabled(); await toggle.click(); await expect(toggle).toHaveAttribute('aria-pressed', before!);
		await expect(toggle).toBeEnabled(); await page.keyboard.press('Tab'); await toggle.focus(); await settled(page);
		expect(await toggle.evaluate((element) => ({ width: getComputedStyle(element).outlineWidth, offset: getComputedStyle(element).outlineOffset }))).toEqual({ width: '2px', offset: '2px' });
		const output = process.env.HELD_COMMENT_DELIVERY_FRAMES_OUT;
		if (output) { await settled(page); await page.screenshot({ path: path.join(output, `normal-toggle-settings-${width}-${theme}.png`), fullPage: false }); }
	});
}
