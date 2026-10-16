import { expect, test } from '@playwright/test';
import { apcaLc, parseColor } from '../../src/lib/utils/contrast';
import { ACCENT_ROLE_TOKENS, accentRoles, FILL_INK_MIN_LC } from '../../src/lib/utils/projectColors';
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
	const actual = await page.evaluate((tokens) => {
		const style = getComputedStyle(document.documentElement);
		return Object.fromEntries(
			Object.entries(tokens).map(([role, token]) => [role, style.getPropertyValue(token).trim()]),
		);
	}, ACCENT_ROLE_TOKENS);

	expect(actual).toEqual(expected);
	expect(Math.abs(apcaLc(parseColor(actual.onFill)!, parseColor(actual.fill)!))).toBeGreaterThanOrEqual(FILL_INK_MIN_LC);
});
