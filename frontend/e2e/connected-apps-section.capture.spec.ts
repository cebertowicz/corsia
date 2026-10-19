import { mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection } from './support/corsia';

const outputDir = resolve(dirname(fileURLToPath(import.meta.url)), process.env.CORSIA_CAPTURE_DIR ?? 'test-results/google-account-rows');
const captureViewports = {
	'chromium-desktop': { width: 1440, height: 1000, label: '1440' },
	'chromium-mobile': { width: 393, height: 852, label: '393' },
} as const;
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
		{ ...healthy, id: 'google-alpha', address: 'alexandria.long-project-alpha-address@example.com' },
		{ ...personal, id: 'google-beta', address: 'alexandria.long-project-beta-address@example.com' },
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

async function expectPaintedClosedTitles(page: Page) {
	const measurements = await page.locator('[data-connected-account-id]').evaluateAll((rows) => rows.map((row) => {
		const provider = row.querySelector('.account-provider');
		const prefix = row.querySelector('.account-local-prefix');
		const suffix = row.querySelector('.account-local-suffix');
		const domain = row.querySelector('.account-domain');
		const title = row.querySelector('.account-closed-name');
		if (!provider || !prefix || !suffix || !domain || !title) return null;
		const providerRange = document.createRange();
		providerRange.selectNodeContents(provider);
		const prefixRange = document.createRange();
		prefixRange.selectNodeContents(prefix);
		const domainRange = document.createRange();
		domainRange.selectNodeContents(domain);
		return { prefix: prefix.textContent, providerRight: providerRange.getBoundingClientRect().right, prefixLeft: prefixRange.getBoundingClientRect().left, prefixRight: prefixRange.getBoundingClientRect().right, suffixLeft: suffix.getBoundingClientRect().left, domainRight: domainRange.getBoundingClientRect().right, titleRight: title.getBoundingClientRect().right };
	}));
	expect(measurements.every((measurement) => measurement && measurement.prefixLeft > measurement.providerRight && measurement.prefixRight <= measurement.suffixLeft + 0.5 && measurement.domainRight <= measurement.titleRight + 0.5)).toBe(true);
	return measurements;
}

async function expectPaintedLongNamesToStaySeparate(page: Page, requireDistinctPrefix: boolean) {
	const measurements = await expectPaintedClosedTitles(page);
	expect(measurements).toHaveLength(4);
	if (requireDistinctPrefix) expect(new Set(measurements.map((measurement) => measurement?.prefix)).size).toBe(4);
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
			const viewport = captureViewports[testInfo.project.name as keyof typeof captureViewports];
			if (!viewport) throw new Error(`No capture viewport for ${testInfo.project.name}`);
			await page.setViewportSize(viewport);
			await openConnectedApps(page);
			const width = viewport.label;
			await expect(page.getByRole('button', { name: 'Google — sam.work@example.com', exact: true })).toBeVisible();
			await expect(page.getByText('Connected · Only CORSIA')).toBeVisible();
			await expectPaintedClosedTitles(page);
			await capture(page, testInfo, `01-migrated-two-accounts-${width}-${theme}`);

			await page.getByRole('button', { name: 'Google — sam.work@example.com', exact: true }).click();
			await expect(page.locator('.capabilities')).toHaveText('ReachesEmail, Calendar, Files');
			await expect(page.getByRole('button', { name: 'Where it works…' })).toBeVisible();
			await page.mouse.move(0, 0);
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
			await page.getByRole('button', { name: 'Google — sam.work@example.com', exact: true }).click();
			const signInAction = page.locator('[data-connected-account-id="google-work"]').getByRole('button', { name: 'Sign in' });
			await expect(signInAction).toBeVisible();
			await capture(page, testInfo, `11-needs-sign-in-${width}-${theme}`);
			await page.evaluate(() => { window.location.hash = 'connected-apps/google-work'; });
			const signInMarker = page.locator('[data-connected-account-id="google-work"] [data-chat-link-focus]');
			await expect(signInMarker).toHaveCount(1);
			await expect(signInAction).toBeFocused();
			await expect(signInAction).toHaveCSS('outline-style', 'solid');
			await capture(page, testInfo, `23-needs-sign-in-chat-link-focus-${width}-${theme}`);
			await page.evaluate(() => { history.replaceState(null, '', window.location.pathname); });

			state = 'signed-out';
			await page.reload();
			await openConnectedApps(page);
			await expect(page.getByText('Signed out · Only CORSIA')).toBeVisible();
			await capture(page, testInfo, `21-signed-out-deliberate-${width}-${theme}`);

			state = 'long';
			await page.reload();
			await openConnectedApps(page);
			await expect(page.getByRole('button', { name: 'Google — shared.prefix.very-long-work-address@example.com', exact: true })).toBeVisible();
			await expectPaintedLongNamesToStaySeparate(page, width === '1440');
			await capture(page, testInfo, `24-same-prefix-long-closed-${width}-${theme}`);
			await page.getByRole('button', { name: 'Google — shared.prefix.very-long-work-address@example.com', exact: true }).click();
			const openLongName = page.locator('[data-connected-account-id="google-work"] .name[data-wrap-name="true"]');
			await expect(openLongName).toHaveText('Google — shared.prefix.very-long-work-address@example.com');
			await expect.poll(() => openLongName.evaluate((node) => node.scrollWidth <= node.clientWidth && node.scrollHeight <= node.clientHeight)).toBe(true);
			await capture(page, testInfo, `17-long-open-${width}-${theme}`);

			state = 'typical';
			await page.reload();
			await openConnectedApps(page);
			await page.getByRole('button', { name: 'Google — sam.work@example.com', exact: true }).click();
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
			await page.getByRole('button', { name: 'Sign out' }).focus();
			await expect(page.locator('[data-chat-link-focus]')).toHaveCount(0);

			failNextList = true;
			await page.evaluate(() => window.dispatchEvent(new Event('focus')));
			await expect(page.getByText('Showing last known items.')).toBeVisible();
			await expect(page.getByText('Actions are unavailable while offline. Try again when you’re connected.')).toBeVisible();
			const offlineAccount = page.locator('[data-connected-account-id="google-work"]');
			await expect(offlineAccount.getByRole('button', { name: 'Where it works…' })).toBeDisabled();
			await expect(offlineAccount.getByRole('button', { name: 'Sign out' })).toBeDisabled();
			await expect(offlineAccount.getByRole('button', { name: 'Remove' })).toBeDisabled();
			await page.evaluate(() => { window.location.hash = 'connected-apps/google-work'; });
			await expect(offlineAccount.locator('[data-chat-link-focus]')).toHaveCount(0);
			await capture(page, testInfo, `offline-${width}-${theme}`);
		});
	}
});
