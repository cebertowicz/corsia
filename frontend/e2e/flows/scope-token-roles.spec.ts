import { expect, test } from '@playwright/test';
import { accentRoles } from '../../src/lib/utils/projectColors';
import { loginByUrl, TEST_TOKEN } from '../support/corsia';

const tok = encodeURIComponent(TEST_TOKEN);

test('project scope applies computed accent roles', async ({ page, request }) => {
	test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'project scope selection is covered on desktop');
	const color = '#f97316';
	const name = `scope-role-${Date.now()}`;
	const created = await request.post(`/api/projects?token=${tok}`, {
		data: { name, status: 'active', accent_color: color },
	});
	expect(created.ok()).toBeTruthy();

	await loginByUrl(page, { waitForComposer: false });
	await page.getByRole('button', { name: /Jump to/ }).click();
	const project = page.locator('.palette .res').filter({ hasText: name });
	await expect(project).toBeVisible({ timeout: 10_000 });
	await project.click();
	await expect(page.getByRole('button', { name: 'Back to workspace' })).toBeVisible({ timeout: 10_000 });

	const theme = await page.locator('html').getAttribute('data-theme');
	expect(theme === 'light' || theme === 'dark').toBe(true);
	const expected = accentRoles(color, theme as 'light' | 'dark');
	const actual = await page.evaluate(() => {
		const style = getComputedStyle(document.documentElement);
		return {
			accent: style.getPropertyValue('--accent').trim(),
			legacyAccent: style.getPropertyValue('--violet').trim(),
			legacyText: style.getPropertyValue('--violet-text').trim(),
			onFill: style.getPropertyValue('--accent-on-fill').trim(),
			scopeText: style.getPropertyValue('--accent-scope-text').trim(),
		};
	});

	expect(actual).toEqual({
		accent: color,
		legacyAccent: color,
		legacyText: expected.text,
		onFill: expected.onFill,
		scopeText: expected.text,
	});
});
