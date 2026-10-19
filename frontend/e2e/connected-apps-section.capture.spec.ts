import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection } from './support/corsia';

const outputDir = process.env.CORSIA_CAPTURE_DIR ?? 'test-results/google-account-rows';
const captureEnabled = process.env.CORSIA_CAPTURE_CONNECTED_APPS === '1';

type AccountState = 'typical' | 'needs-sign-in' | 'signed-out' | 'long';
type Account = {
	id: string;
	provider: 'google';
	address: string;
	capabilities: ('mail' | 'calendar' | 'files')[];
	state: 'connected' | 'needs_sign_in' | 'signed_out';
	reach: { mode: 'all' | 'selected'; project_ids: string[]; outside_projects: boolean };
};

const onlyCorsia = { mode: 'selected' as const, project_ids: [], outside_projects: false };
const allProjects = { mode: 'all' as const, project_ids: [], outside_projects: false };
const healthy: Account = { id: 'google-work', provider: 'google', address: 'sam.work@example.com', capabilities: ['mail', 'calendar', 'files'], state: 'connected', reach: onlyCorsia };
const personal: Account = { id: 'google-personal', provider: 'google', address: 'sam@example.com', capabilities: ['mail', 'calendar', 'files'], state: 'connected', reach: allProjects };

function accountsFor(state: AccountState): Account[] {
	if (state === 'needs-sign-in') return [{ ...healthy, state: 'needs_sign_in' }, personal];
	if (state === 'signed-out') return [{ ...healthy, state: 'signed_out' }, personal];
	if (state === 'long') return [
		{ ...healthy, address: 'shared.prefix.very-long-work-address@example.com' },
		{ ...personal, address: 'shared.prefix.very-long-personal-address@example.com' },
	];
	return [healthy, personal];
}

async function capture(page: Page, testInfo: TestInfo, name: string) {
	// Capture the settled viewport; full-page captures intermittently paint a blank desktop document after Settings reloads.
	await page.waitForTimeout(400);
	mkdirSync(outputDir, { recursive: true });
	await page.screenshot({ path: join(outputDir, `${name}-${testInfo.project.name}.png`), fullPage: false, animations: 'disabled' });
}

async function openConnectedApps(page: Page) {
	await openSettings(page);
	await openSettingsSection(page, 'Connected apps');
	await expect(page.getByRole('heading', { name: 'Connected apps' })).toBeVisible();
}

async function expectPaintedLongNamesToStaySeparate(page: Page) {
	const measurements = await page.locator('[data-connected-account-id]').evaluateAll((rows) => rows.map((row) => {
		const prefix = row.querySelector('.account-local-prefix');
		const suffix = row.querySelector('.account-local-suffix');
		if (!prefix || !suffix) return null;
		const range = document.createRange();
		range.selectNodeContents(prefix);
		return { prefix: prefix.textContent, paintedRight: range.getBoundingClientRect().right, nextLeft: suffix.getBoundingClientRect().left };
	}));
	expect(measurements).toHaveLength(2);
	expect(measurements.every((measurement) => measurement && measurement.prefix?.includes('…') && measurement.paintedRight <= measurement.nextLeft + 0.5)).toBe(true);
	expect(new Set(measurements.map((measurement) => measurement?.prefix)).size).toBe(2);
}

test.describe('Google account rows capture', () => {
	test.skip(!captureEnabled, 'Capture-only proof; enable explicitly when producing the evidence pack.');
	test.setTimeout(120_000);

	for (const theme of ['light', 'dark'] as const) {
		test(`captures account states in ${theme}`, async ({ page }, testInfo) => {
			let state: AccountState = 'typical';
			let failNextList = false;
			await page.route('**/api/mcp/servers**', async (route) => {
				if (route.request().method() !== 'GET') return route.continue();
				if (failNextList) {
					failNextList = false;
					return route.fulfill({ status: 503, json: { detail: 'offline' } });
				}
				await route.fulfill({ json: {
					accounts: accountsFor(state),
					servers: [{ id: 'notes', name: 'Notes', state: 'working', tool_count: 2, reach: allProjects }],
					sign_in_here: false,
				} });
			});
			await page.route('**/api/mcp/accounts/**', async (route) => {
				const request = route.request();
				if (new URL(request.url()).pathname.endsWith('/sign-out')) return route.fulfill({ json: { row: { ...healthy, state: 'signed_out' }, revocation: 'provider_unreachable' } });
				if (request.method() === 'DELETE') return route.fulfill({ json: { id: 'google-work', removed: true, revocation: 'provider_unreachable' } });
				if (request.method() === 'PUT') return route.fulfill({ json: { row: { ...healthy, reach: allProjects } } });
				await route.continue();
			});
			await page.route('**/api/projects**', (route) => route.fulfill({ json: { projects: [{ id: 'garden', name: 'Garden plan', status: 'active', accent_color: null }] } }));
			await page.emulateMedia({ colorScheme: theme });
			await loginByUrl(page, { waitForComposer: false });
			await openConnectedApps(page);
			const viewport = page.viewportSize()!.width;
			const width = viewport > 768 ? '1440' : '393';
			await expect(page.getByRole('button', { name: 'Google — sam.work@example.com' })).toBeVisible();
			await expect(page.getByText('Connected · Only CORSIA')).toBeVisible();
			await capture(page, testInfo, `01-migrated-two-accounts-${width}-${theme}`);

			await page.getByRole('button', { name: 'Google — sam.work@example.com' }).click();
			await expect(page.getByText('Reaches — Email, Calendar, Files')).toBeVisible();
			await expect(page.getByRole('button', { name: 'Where it works…' })).toBeVisible();
			await capture(page, testInfo, `02-open-actions-${width}-${theme}`);

			await page.getByRole('button', { name: 'Where it works…' }).click();
			const whereSheet = page.locator('.modal').filter({ hasText: 'Where Google — sam.work@example.com works' });
			await expect(whereSheet).toBeVisible();
			await capture(page, testInfo, `04-where-it-works-${width}-${theme}`);
			await whereSheet.getByRole('button', { name: 'Cancel' }).click();

			await page.getByRole('button', { name: 'Sign out' }).click();
			const signOutDialog = page.locator('.modal').filter({ hasText: 'Sign out of Google — sam.work@example.com?' });
			await expect(signOutDialog).toBeVisible();
			await capture(page, testInfo, `06-sign-out-confirm-${width}-${theme}`);
			await signOutDialog.getByRole('button', { name: 'Sign out' }).click();
			await expect(page.getByText('couldn’t reach Google')).toBeVisible();
			await capture(page, testInfo, `20-signed-out-google-step-${width}-${theme}`);

			state = 'needs-sign-in';
			await page.reload();
			await openConnectedApps(page);
			await expect(page.getByText('Needs you to sign in · Only CORSIA')).toBeVisible();
			await page.getByRole('button', { name: 'Google — sam.work@example.com' }).click();
			await expect(page.locator('[data-connected-account-id="google-work"]').getByRole('button', { name: 'Sign in' })).toBeVisible();
			await capture(page, testInfo, `11-needs-sign-in-${width}-${theme}`);

			state = 'signed-out';
			await page.reload();
			await openConnectedApps(page);
			await expect(page.getByText('Signed out · Only CORSIA')).toBeVisible();
			await capture(page, testInfo, `21-signed-out-deliberate-${width}-${theme}`);

			state = 'long';
			await page.reload();
			await openConnectedApps(page);
			await expect(page.getByRole('button', { name: 'Google — shared.prefix.very-long-work-address@example.com' })).toBeVisible();
			if (viewport <= 768) await expectPaintedLongNamesToStaySeparate(page);
			await capture(page, testInfo, `24-same-prefix-long-closed-${width}-${theme}`);
			await page.getByRole('button', { name: 'Google — shared.prefix.very-long-work-address@example.com' }).click();
			await capture(page, testInfo, `17-long-open-${width}-${theme}`);

			state = 'typical';
			await page.reload();
			await openConnectedApps(page);
			await page.getByRole('button', { name: 'Google — sam.work@example.com' }).click();
			await page.getByRole('button', { name: 'Remove' }).click();
			const removeDialog = page.locator('.modal').filter({ hasText: 'Remove Google — sam.work@example.com?' });
			await expect(removeDialog).toBeVisible();
			await capture(page, testInfo, `13-remove-confirm-${width}-${theme}`);
			await removeDialog.getByRole('button', { name: 'Cancel' }).click();

			await page.evaluate(() => { window.location.hash = 'connected-apps/google-work'; });
			const linkedAction = page.locator('[data-chat-link-focus] .btn');
			await expect(linkedAction).toBeFocused();
			await expect(linkedAction).toHaveCSS('outline-style', 'solid');
			await capture(page, testInfo, `18-chat-link-focus-${width}-${theme}`);

			failNextList = true;
			await page.evaluate(() => window.dispatchEvent(new Event('focus')));
			await expect(page.getByText('Showing last known items.')).toBeVisible();
			await capture(page, testInfo, `offline-${width}-${theme}`);
		});
	}
});
