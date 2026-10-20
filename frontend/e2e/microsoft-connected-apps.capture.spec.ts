import { mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection } from './support/corsia';

const outputDir = resolve(dirname(fileURLToPath(import.meta.url)), process.env.CORSIA_CAPTURE_DIR ?? 'test-results/microsoft-connected-apps');
const captureEnabled = process.env.CORSIA_CAPTURE_MICROSOFT_CONNECTED_APPS === '1';
const clientId = '11111111-1111-4111-8111-111111111111';
const tenantId = '22222222-2222-4222-8222-222222222222';
const allProjects = { mode: 'all' as const, project_ids: [], outside_projects: false };
const onlyCorsia = { mode: 'corsia' as const, project_ids: [], outside_projects: false };
type State = 'connected' | 'admin' | 'cancelled' | 'expired' | 'failure' | 'registration' | 'many';

function microsoft(state: State, id = 'microsoft-work', address = 'alex@contoso.example') {
	return {
		id, provider: 'microsoft' as const, provider_display_name: 'Microsoft', address, capabilities: ['mail', 'calendar', 'files'],
		state: state === 'connected' || state === 'many' ? 'connected' as const : 'needs_sign_in' as const,
		account_kind: address.endsWith('@outlook.example') ? 'personal' as const : 'work' as const,
		cause: state === 'admin' ? 'admin_approval_required' : state === 'cancelled' ? 'consent_cancelled' : state === 'expired' ? 'refresh_token_invalid' : state === 'failure' ? 'provider_unavailable' : state === 'registration' ? 'registration_misconfigured' : null,
		client_id: clientId, tenant_id: tenantId, reach: allProjects,
	};
}

function accounts(state: State) {
	if (state === 'many') return [
		microsoft(state, 'microsoft-one', 'shared.prefix.very-long-work-address@contoso.example'),
		microsoft(state, 'microsoft-two', 'shared.prefix.very-long-personal-address@contoso.example'),
		microsoft(state, 'microsoft-three', 'alexandria.long-project-alpha-address@contoso.example'),
		microsoft(state, 'microsoft-four', 'alexandria.long-project-beta-address@contoso.example'),
	];
	return [microsoft(state), { id: 'google', provider: 'google' as const, provider_display_name: 'Google', address: 'alex@gmail.example', capabilities: ['mail', 'calendar', 'files'], state: 'connected' as const, tenant_id: null, reach: onlyCorsia }];
}

async function capture(page: Page, testInfo: TestInfo, name: string) {
	await page.waitForTimeout(300);
	mkdirSync(outputDir, { recursive: true });
	await page.screenshot({ path: join(outputDir, `${name}-${testInfo.project.name}.png`), fullPage: false, animations: 'disabled' });
}

async function openConnectedApps(page: Page) {
	await openSettings(page);
	await openSettingsSection(page, 'Connected apps');
	await expect(page.getByRole('heading', { name: 'Connected apps' })).toBeVisible();
}

test.describe('Microsoft Connected apps capture', () => {
	test.skip(!captureEnabled, 'Capture-only proof; enable explicitly when producing the evidence pack.');
	test.setTimeout(120_000);

	for (const theme of ['light', 'dark'] as const) {
		test(`captures Microsoft account states in ${theme}`, async ({ page }, testInfo) => {
			let state: State = 'connected';
			let failNextList = false;
			await page.route('**/api/mcp/servers**', async (route) => {
				if (route.request().method() !== 'GET') return route.continue();
				if (failNextList) { failNextList = false; return route.fulfill({ status: 503, json: { detail: 'offline' } }); }
				return route.fulfill({ json: { accounts: accounts(state), servers: [], sign_in_here: false } });
			});
			await page.route('**/api/mcp/accounts/**', async (route) => {
				if (route.request().method() === 'DELETE') return route.fulfill({ json: { id: 'microsoft-work', removed: true, revocation: 'manual_only' } });
				return route.continue();
			});
			await page.route('**/api/projects**', (route) => route.fulfill({ json: { projects: [] } }));
			await page.emulateMedia({ colorScheme: theme });
			await loginByUrl(page, { waitForComposer: false });
			await page.context().grantPermissions(['clipboard-write'], { origin: new URL(page.url()).origin });
			const label = testInfo.project.name.includes('mobile') ? '393' : '1440';
			await page.setViewportSize(label === '393' ? { width: 393, height: 852 } : { width: 1440, height: 1000 });
			await openConnectedApps(page);
			const open = async (name = 'Microsoft — alex@contoso.example') => {
				await page.getByRole('button', { name, exact: true }).click();
			};

			await expect(page.getByText('Connected · All projects')).toBeVisible();
			await capture(page, testInfo, `ms-connected-${label}-${theme}`);
			await open();
			const whereItWorks = page.getByRole('button', { name: 'Where it works…' });
			await whereItWorks.focus();
			await page.keyboard.press('Tab');
			await page.keyboard.press('Shift+Tab');
			await expect(whereItWorks).toBeFocused();
			await expect(whereItWorks).toHaveCSS('outline-style', 'solid');
			await capture(page, testInfo, `ms-focus-${label}-${theme}`);

			state = 'admin';
			await page.reload(); await openConnectedApps(page); await open();
			await expect(page.getByText('Your organization needs to approve CORSIA before it can connect this account. Send this to your IT admin, then press Sign in.')).toBeVisible();
			await page.getByRole('button', { name: 'Copy message for IT' }).click();
			await expect(page.getByText('Message copied')).toBeVisible();
			await capture(page, testInfo, `ms-admin-copied-${label}-${theme}`);

			state = 'cancelled';
			await page.reload(); await openConnectedApps(page); await open();
			await expect(page.getByText('Sign-in was cancelled. Press Sign in to try again.')).toBeVisible();
			await capture(page, testInfo, `ms-consent-cancelled-${label}-${theme}`);

			state = 'expired';
			await page.reload(); await openConnectedApps(page); await open();
			await expect(page.getByText('Microsoft signed you out, so agents can’t use this account until you sign in again.')).toBeVisible();
			await capture(page, testInfo, `ms-refresh-invalid-${label}-${theme}`);

			state = 'failure';
			await page.reload(); await openConnectedApps(page); await open();
			await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
			await capture(page, testInfo, `ms-provider-failure-${label}-${theme}`);

			state = 'registration';
			await page.reload(); await openConnectedApps(page); await open();
			await expect(page.getByText('Microsoft app needs fixing · All projects')).toBeVisible();
			await expect(page.getByText("Your Microsoft app for CORSIA isn't set up right. Ask CORSIA to fix it, then press Sign in.")).toBeVisible();
			await expect(page.getByRole('button', { name: 'Sign in' })).toHaveClass(/primary/);
			await expect(page.getByRole('button', { name: 'Copy message for IT' })).toHaveCount(0);
			await expect(page.getByRole('link', { name: 'Manage Microsoft permissions' })).toHaveCount(0);
			await capture(page, testInfo, `ms-registration-misconfigured-${label}-${theme}`);

			state = 'connected';
			await page.reload(); await openConnectedApps(page); await open();
			await page.getByRole('button', { name: 'Remove' }).click();
			const dialog = page.locator('.modal').filter({ hasText: 'Remove Microsoft — alex@contoso.example?' });
			await expect(dialog).toBeVisible();
			await capture(page, testInfo, `ms-remove-dialog-${label}-${theme}`);
			await dialog.getByRole('button', { name: 'Remove' }).click();
			await expect(page.getByRole('link', { name: 'Manage Microsoft permissions' })).toHaveAttribute('href', 'https://myapps.microsoft.com');
			await capture(page, testInfo, `ms-post-remove-work-${label}-${theme}`);

			state = 'many';
			await page.reload(); await openConnectedApps(page);
			await expect(page.getByRole('button', { name: 'Microsoft — shared.prefix.very-long-work-address@contoso.example', exact: true })).toBeVisible();
			await capture(page, testInfo, `ms-long-many-${label}-${theme}`);

			failNextList = true;
			await page.evaluate(() => window.dispatchEvent(new Event('focus')));
			await expect(page.getByText('Showing last known items.')).toBeVisible();
			await capture(page, testInfo, `ms-offline-${label}-${theme}`);
		});
	}
});
