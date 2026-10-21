import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect } from '@playwright/test';
import { test } from './support/test';
import { loginByUrl, openSettings } from './support/corsia';

const outputDir = process.env.CORSIA_CAPTURE_DIR ?? 'test-results/set-key-rows';
const captureEnabled = process.env.CORSIA_CAPTURE_SET_KEY_ROWS === '1';

const refs = (entry: string, name = 'API_KEY') => [{ kind: 'env', name, entry, set: false }];
const twoMissingRefs = [
	{ kind: 'env', name: 'LINEAR_PRIMARY_KEY', entry: 'mcp-linear-primary-key-111', set: false },
	{ kind: 'env', name: 'LINEAR_SECONDARY_KEY', entry: 'mcp-linear-secondary-key-222', set: false },
];

function rows(sentryState = 'token_rejected') {
	return [
		{ id: 'stripe', name: 'Stripe', state: 'needs_token', revision: 1, key_refs: refs('mcp-stripe-api-key-123'), reach: { mode: 'all', project_ids: [], outside_projects: false } },
		{ id: 'sentry', name: 'Sentry', state: sentryState, revision: 1, key_refs: refs('mcp-sentry-token-456', 'SENTRY_TOKEN'), reach: { mode: 'all', project_ids: [], outside_projects: false } },
		{ id: 'postgres', name: 'Postgres', state: 'needs_token', revision: 1, key_refs: refs('mcp-postgres-password-789', 'PGPASSWORD'), reach: { mode: 'all', project_ids: [], outside_projects: false } },
		{ id: 'linear', name: 'Linear', state: 'needs_token', revision: 1, key_refs: twoMissingRefs, reach: { mode: 'all', project_ids: [], outside_projects: false } },
	];
}

test.describe('Set key from Connected apps capture', () => {
	test.skip(!captureEnabled, 'Capture-only proof; enable explicitly when producing the evidence pack.');
	test.setTimeout(120_000);

	test('captures closed and open key rows and the focused Keys card', async ({ page }) => {
		mkdirSync(outputDir, { recursive: true });
		let sentryState = 'token_rejected';
		let failSave = false;
		let releaseReconnect: (() => void) | null = null;
		await page.route('**/api/mcp/servers**', async (route) => {
			if (route.request().method() === 'POST') {
				await route.fulfill({ json: { id: 'new-stripe', name: 'Stripe', state: 'needs_token', revision: 1, key_refs: refs('mcp-stripe-api-key-123'), reach: { mode: 'all', project_ids: [], outside_projects: false } } });
				return;
			}
			if (route.request().method() !== 'GET') return route.continue();
			if (sentryState === 'connecting') await new Promise<void>((resolve) => { releaseReconnect = resolve; });
			await route.fulfill({ json: { servers: rows(sentryState), sign_in_here: false } });
		});
		await page.route('**/api/keys**', async (route) => {
			if (route.request().method() !== 'GET') return route.continue();
			await route.fulfill({ json: { keys: [
				{ name: 'mcp-stripe-api-key-123', label: 'Stripe key', description: 'Used by this connected app.', builtin: false, added_by: null, used_by: null, is_set: false, set_at: null, checkable: false, ignored_env_var: null },
				{ name: 'mcp-sentry-token-456', label: 'Sentry key', description: 'Used by this connected app.', builtin: false, added_by: null, used_by: null, is_set: true, set_at: null, checkable: false, ignored_env_var: null },
				{ name: 'mcp-postgres-password-789', label: 'Postgres key', description: 'Used by this connected app.', builtin: false, added_by: null, used_by: null, is_set: false, set_at: null, checkable: false, ignored_env_var: null },
				{ name: 'mcp-linear-primary-key-111', label: 'Linear primary key', description: 'Used by this connected app.', builtin: false, added_by: null, used_by: null, is_set: false, set_at: null, checkable: false, ignored_env_var: null },
				{ name: 'mcp-linear-secondary-key-222', label: 'Linear secondary key', description: 'Used by this connected app.', builtin: false, added_by: null, used_by: null, is_set: false, set_at: null, checkable: false, ignored_env_var: null },
			], provider_keys: [], server_keys: [] } });
		});
		await page.route('**/api/keys/**', async (route) => {
			if (route.request().method() !== 'PUT') return route.continue();
			if (failSave) {
				await route.fulfill({ status: 500, json: { detail: { code: 'write_failed', message: "Couldn't save that. Nothing was changed." } } });
				return;
			}
			await route.fulfill({ json: { key: {} } });
		});

		for (const theme of ['light', 'dark'] as const) {
			for (const viewport of [1440, 393]) {
				await page.setViewportSize({ width: viewport, height: 980 });
				await page.emulateMedia({ colorScheme: theme });
				await loginByUrl(page, { waitForComposer: false });
				await openSettings(page);
				await (viewport > 768 ? page.getByRole('button', { name: 'Connected apps' }) : page.getByRole('button', { name: 'Apps' })).click();
				await expect(page.getByRole('button', { name: /Stripe Needs a key/ })).toBeVisible();
				await page.screenshot({ path: join(outputDir, `key-rows-closed-${viewport}-${theme}.png`), fullPage: true, animations: 'disabled' });

				await page.getByRole('button', { name: /Stripe/ }).click();
				await expect(page.getByText('Then press Set key next to Stripe.')).toBeVisible();
				await page.getByRole('button', { name: /Sentry/ }).click();
				await expect(page.getByText("Sentry didn't accept its key.")).toBeVisible();
				await page.screenshot({ path: join(outputDir, `key-rows-open-${viewport}-${theme}.png`), fullPage: true, animations: 'disabled' });

				await page.getByRole('button', { name: 'Replace key' }).click();
				const headerBack = page.locator('.shell-header .h-back');
				const sentryCard = page.locator('[data-key-entry="mcp-sentry-token-456"]');
				await expect(headerBack).toHaveAccessibleName('Connected apps');
				const sentryAction = sentryCard.getByRole('button', { name: 'Replace' });
				const sentryActions = sentryCard.locator('.card-actions');
				await expect(sentryAction).toBeFocused();
				await expect(sentryActions).toHaveClass(/arrival/);
				expect(await sentryAction.evaluate((element) => getComputedStyle(element).animationName.includes('key-action-arrival'))).toBe(true);
				await page.screenshot({ path: join(outputDir, `key-card-selected-${viewport}-${theme}.png`), fullPage: true, animations: 'disabled' });
				await page.waitForTimeout(120);
				await expect(sentryAction).toBeFocused();
				await expect(sentryActions).toHaveClass(/arrival/);
				if (theme === 'light') {
					const frozenPulse = await page.addStyleTag({ content: '.card-actions.arrival .btn.primary { animation: none !important; opacity: 0.72 !important; transform: scale(0.96) !important; }' });
					await page.screenshot({ path: join(outputDir, `key-card-arrival-mid-pulse-${viewport}-light.png`), fullPage: true });
					await frozenPulse.evaluate((style) => style.remove());
				}
				await page.waitForTimeout(1080);
				await expect(sentryAction).toBeFocused();
				await expect(sentryActions).not.toHaveClass(/arrival/);
				if (theme === 'light') {
					await page.screenshot({ path: join(outputDir, `key-card-arrival-settled-${viewport}-light.png`), fullPage: true, animations: 'disabled' });
				}
				await expect(sentryCard).not.toHaveClass(/focused/);

				await sentryCard.getByRole('button', { name: 'Replace' }).click();
				await expect(sentryCard.getByLabel('Key')).toBeVisible();
				if (theme === 'light') {
					await page.screenshot({ path: join(outputDir, `key-card-entry-open-${viewport}-light.png`), fullPage: true, animations: 'disabled' });
				}
				await sentryCard.getByRole('button', { name: 'Cancel' }).click();

				sentryState = 'connecting';
				await sentryCard.getByRole('button', { name: 'Replace' }).click();
				await page.getByLabel('Key').fill('prototype-only-value');
				await page.getByLabel('CORSIA password').fill('prototype-password');
				await sentryCard.getByRole('button', { name: 'Save' }).click();
				await expect(page.getByText('Saved. Reconnecting Sentry…')).toBeVisible();
				await page.screenshot({ path: join(outputDir, `key-reconnecting-${viewport}-${theme}.png`), fullPage: true, animations: 'disabled' });

				sentryState = 'working';
				releaseReconnect?.();
				await expect(page.getByText('Sentry is connected.')).toBeVisible();
				await page.screenshot({ path: join(outputDir, `key-connected-${viewport}-${theme}.png`), fullPage: true, animations: 'disabled' });

				sentryState = 'token_rejected';
				await sentryCard.getByRole('button', { name: 'Replace' }).click();
				await page.getByLabel('Key').fill('prototype-only-value');
				await page.getByLabel('CORSIA password').fill('prototype-password');
				await sentryCard.getByRole('button', { name: 'Save' }).click();
				await expect(page.getByText("Sentry didn't accept this key.")).toBeVisible();
				await page.screenshot({ path: join(outputDir, `key-rejected-${viewport}-${theme}.png`), fullPage: true, animations: 'disabled' });

				await sentryCard.getByRole('button', { name: 'Replace' }).click();
				await page.getByLabel('Key').fill('prototype-only-value');
				await page.getByLabel('CORSIA password').fill('prototype-password');
				failSave = true;
				await sentryCard.getByRole('button', { name: 'Save' }).click();
				await expect(page.getByText("Couldn't save that. Nothing was changed.")).toBeVisible();
				if (theme === 'light') {
					await page.screenshot({ path: join(outputDir, `key-save-failed-${viewport}-light.png`), fullPage: true, animations: 'disabled' });
				}
				failSave = false;
				await sentryCard.getByRole('button', { name: 'Cancel' }).click();

				await headerBack.click();
				if (viewport <= 768) {
					await expect(page.getByRole('button', { name: /Stripe Needs a key/ })).toBeVisible();
				}
				await page.getByRole('button', { name: 'Connect an app' }).click();
				await page.getByRole('button', { name: 'Continue' }).click();
				await page.getByLabel('What it runs').fill('npx -y @example/stripe');
				await page.getByLabel('Name').fill('Stripe');
				await page.getByRole('button', { name: 'Continue' }).click();
				await page.getByRole('button', { name: 'Connect', exact: true }).click();
				await expect(page.getByRole('heading', { name: 'Stripe: Needs a key' })).toBeVisible();
				await page.getByRole('dialog').getByRole('button', { name: 'Set key' }).click();
				const stripeCard = page.locator('[data-key-entry="mcp-stripe-api-key-123"]');
				await expect(page.locator('.shell-header .h-back')).toHaveAccessibleName('Connected apps');
				await expect(stripeCard.getByRole('button', { name: 'Set' })).toBeFocused();
				await page.screenshot({ path: join(outputDir, `connect-result-key-${viewport}-${theme}.png`), fullPage: true, animations: 'disabled' });

				await page.locator('.shell-header .h-back').click();
				const linearRow = page.locator('[data-settings-row="Linear"]');
				await linearRow.getByRole('button', { name: 'Set key' }).click();
				const primaryLinearCard = page.locator('[data-key-entry="mcp-linear-primary-key-111"]');
				const secondaryLinearCard = page.locator('[data-key-entry="mcp-linear-secondary-key-222"]');
				// `openKey` selects the first unset descriptor in server order (`find(!set) ?? refs[0]`).
				await expect(primaryLinearCard.getByRole('heading', { name: 'Linear key: LINEAR_PRIMARY_KEY' })).toBeVisible();
				await expect(primaryLinearCard.getByRole('button', { name: 'Set' })).toBeFocused();
				await expect(secondaryLinearCard.getByRole('heading', { name: 'Linear key: LINEAR_SECONDARY_KEY' })).toBeVisible();
				if (theme === 'light') {
					await page.screenshot({ path: join(outputDir, `key-cards-two-missing-selected-${viewport}-light.png`), fullPage: true, animations: 'disabled' });
				}
			}
		}
	});
});
