import { expect, test, type Page } from '@playwright/test';
import { loginByUrl, openSettingsSection } from '../support/corsia';

type ScreenId = 'W1' | 'R1' | 'R2' | 'R3' | 'R4' | 'R5';

const SCREENS: readonly ScreenId[] = ['W1', 'R1', 'R2', 'R3', 'R4', 'R5'];

function surfacesFor(projectName: string) {
	if (projectName === 'chromium-desktop') {
		return [
			['desktop-light', 1440, 900, 'light'],
			['desktop-dark', 1440, 900, 'dark'],
		] as const;
	}
	return [
		['phone-light', 390, 844, 'light'],
		['phone-dark', 390, 844, 'dark'],
	] as const;
}

function vendorAccounts() {
	const account = (id: string, provider: string, name: string, email: string, fiveHourUsed: number, weeklyUsed: number) => ({
		id, provider, name, is_default: false, state: 'signed_in', revision: 1, identity: {},
		label: { name, email, text: `${name} · ${email}`, custom: null },
		usage: { state: 'allowed', usable: true, windows: [
			{ kind: 'five_hour', state: 'reported', used_percent: fiveHourUsed, window_minutes: 300 },
			{ kind: 'seven_day', state: 'reported', used_percent: weeklyUsed, window_minutes: 10080 },
		], checking: false, check: null },
	});
	return [
		account('personal', 'claude-code', 'Personal Max', 'sam@example.com', 62, 41),
		account('team', 'claude-code', 'Acme (Team)', 'sam@example.com', 42, 78),
		account('pro', 'codex', 'Pro', 'sam@example.com', 12, 38),
		account('lite', 'codex', 'Pro Lite', 'sam.studio@example.com', 9, 52),
	];
}

function providers(screen: ScreenId) {
	const entry = (provider_key: string, units: { id: string; label: string; enabled: boolean }[], disabled: string[] = []) => ({
		provider_key,
		capabilities: { units: 'accounts' as const, rollover: true },
		// The approved mockups show Claude Code rolling over and Codex not.
		rollover: provider_key === 'claude-code',
		order: units.length > 1 ? units.map((unit) => unit.id) : null,
		disabled,
		revision: 1,
		units,
	});
	const claude = [
		{ id: 'personal', label: 'Personal Max', enabled: screen !== 'R3' },
		{ id: 'team', label: 'Acme (Team)', enabled: true },
	];
	const codex = [
		{ id: 'pro', label: 'Pro', enabled: true },
		{ id: 'lite', label: 'Pro Lite', enabled: true },
	];
	if (screen === 'R4') return [entry('claude-code', []), entry('codex', codex)];
	if (screen === 'R5') return [entry('claude-code', [claude[1]]), entry('codex', [codex[0]])];
	return [entry('claude-code', claude, screen === 'R3' ? ['personal'] : []), entry('codex', codex)];
}

function policyFor(screen: ScreenId) {
	if (screen === 'W1') {
		return {
			scope: 'workspace',
			providers: providers(screen),
			overriding_projects: [{ id: 'atlas', name: 'Atlas migration' }, { id: 'notes', name: 'Field notes' }],
		};
	}
	return {
		scope: 'project',
		use_workspace: screen === 'R1',
		override_revision: 1,
		providers: providers(screen),
	};
}

async function servePolicyStates(page: Page, state: { screen: ScreenId }) {
	await page.route('**/api/vendor-accounts**', async (route) => {
		if (route.request().method() === 'GET') await route.fulfill({ json: { accounts: vendorAccounts() } });
		else await route.continue();
	});
	await page.route('**/api/provider-policy**', async (route) => {
		const request = route.request();
		const pathname = new URL(request.url()).pathname;
		if (request.method() === 'GET' && pathname === '/api/provider-policy') {
			await route.fulfill({ json: policyFor(state.screen) });
			return;
		}
		if (request.method() === 'PUT') {
			const provider = (policyFor(state.screen).providers).find((item) => pathname.endsWith(item.provider_key));
			await route.fulfill({ json: provider ?? policyFor(state.screen) });
			return;
		}
		await route.continue();
	});
}

async function openActivity(page: Page) {
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	}
	await page.getByRole('button', { name: 'Activity', exact: true }).click();
	await expect(page.getByRole('heading', { name: 'Activity', exact: true })).toBeVisible();
}

async function createProjectAndOpenSettings(page: Page, name: string) {
	await page.keyboard.press('Control+k');
	await expect(page.locator('.palette')).toBeVisible();
	await page.locator('.palette .res').filter({ hasText: 'New project' }).click();
	const modal = page.getByRole('dialog');
	await expect(modal).toBeVisible();
	await modal.getByRole('textbox', { name: 'Name' }).fill(name);
	await modal.getByRole('button', { name: 'Create project' }).click();
	await expect(page.getByRole('button', { name: 'Project settings' })).toBeVisible({ timeout: 10_000 });
	await page.getByRole('button', { name: 'Project settings' }).click();
	await expect(page.getByRole('textbox', { name: 'Name', exact: true })).toHaveValue(name);
}

test.describe('@surface:provider-policy Slice 2 subscription capture matrix', () => {
	test('captures W1 and R1–R5 at the approved viewports and themes', async ({ page }, testInfo) => {
		test.setTimeout(120_000);
		const state: { screen: ScreenId } = { screen: 'W1' };
		await servePolicyStates(page, state);

		for (const [surface, width, height, colorScheme] of surfacesFor(testInfo.project.name)) {
			await page.setViewportSize({ width, height });
			await page.emulateMedia({ colorScheme });
			for (const screen of SCREENS) {
				state.screen = screen;
				await loginByUrl(page, { waitForComposer: false });
				if (screen === 'W1') {
					await openActivity(page);
					await page.getByRole('button', { name: 'Workspace settings' }).click();
					await openSettingsSection(page, 'Providers');
					const region = page.getByRole('region', { name: 'Workspace providers' });
					await expect(region).toBeVisible();
					await expect(region.getByText('Overridden in 2 projects: Atlas migration, Field notes')).toHaveCount(2);
					await expect(region.getByText('Personal Max · sam@example.com')).toBeVisible();
					await expect(region.getByLabel('Account usage').first()).toHaveText('5h 38% left · wk 59% left');
					await expect(region.getByRole('button', { name: 'Move Pro up' })).toHaveCount(0);
					await region.scrollIntoViewIfNeeded();
					await expect(region.getByRole('button', { name: 'Move Personal Max up' })).toBeVisible();
				} else {
					await openActivity(page);
					await createProjectAndOpenSettings(page, `subscription-${screen}-${surface}`);
					const region = page.getByRole('region', { name: 'Project providers' });
					await expect(region).toBeVisible();
					if (screen === 'R1') await expect(region.getByRole('button', { name: 'Use workspace settings' })).toHaveAttribute('aria-pressed', 'true');
					if (screen === 'R2') {
						await expect(region.getByText("Turning this on replaces this project's settings with the workspace's. Turning it off again starts from the workspace values.")).toBeVisible();
						await expect(region.getByRole('button', { name: 'Open workspace providers' })).toBeVisible();
					}
					if (screen === 'R3') await expect(region.getByRole('button', { name: 'Personal Max', exact: true })).toHaveAttribute('aria-pressed', 'false');
					if (screen === 'R4') await expect(region.getByRole('button', { name: 'Sign in' })).toBeVisible();
					await region.evaluate((element) => element.scrollIntoView({ block: 'start' }));
					if (screen === 'R5') {
						await expect(region.getByRole('button', { name: 'Acme (Team)' })).toBeVisible();
						await expect(region.getByRole('button', { name: 'Pro', exact: true })).toBeVisible();
						await expect(region.getByRole('button', { name: 'Roll over when an account hits its limit' })).toHaveCount(0);
						await expect(region.getByRole('button', { name: /Move / })).toHaveCount(0);
					}
				}
				await page.screenshot({ path: testInfo.outputPath(`${screen}-${surface}.png`), animations: 'disabled', scale: 'css' });
				if (screen === 'R4') {
					await page.getByRole('button', { name: 'Sign in' }).click();
					await expect(page.getByRole('heading', { name: 'Accounts and rollover', exact: true })).toBeVisible();
				}
			}
		}
	});

	test('keeps two account toggles usable after each policy save', async ({ page }, testInfo) => {
		test.setTimeout(120_000);
		let policy = policyFor('W1');
		let saves = 0;
		await page.route('**/api/vendor-accounts**', async (route) => {
			if (route.request().method() === 'GET') await route.fulfill({ json: { accounts: vendorAccounts() } });
			else await route.continue();
		});
		await page.route('**/api/provider-policy**', async (route) => {
			const request = route.request();
			const pathname = new URL(request.url()).pathname;
			if (request.method() === 'GET' && pathname === '/api/provider-policy') {
				await route.fulfill({ json: policy });
				return;
			}
			if (request.method() === 'PUT') {
				const body = request.postDataJSON() as { disabled: string[] };
				const provider = policy.providers.find((item) => pathname.endsWith(item.provider_key));
				if (!provider) throw new Error(`Unknown provider policy path: ${pathname}`);
				const saved = {
					...provider,
					disabled: body.disabled,
					revision: provider.revision + 1,
					units: provider.units.map((unit) => ({ ...unit, enabled: !body.disabled.includes(unit.id) })),
				};
				policy = { ...policy, providers: policy.providers.map((item) => item.provider_key === saved.provider_key ? saved : item) };
				saves += 1;
				await route.fulfill({ json: saved });
				return;
			}
			await route.continue();
		});

		for (const [surface, width, height, colorScheme] of surfacesFor(testInfo.project.name)) {
			policy = policyFor('W1');
			saves = 0;
			await page.setViewportSize({ width, height });
			await page.emulateMedia({ colorScheme });
			await loginByUrl(page, { waitForComposer: false });
			await openActivity(page);
			await page.getByRole('button', { name: 'Workspace settings' }).click();
			await openSettingsSection(page, 'Providers');
			const region = page.getByRole('region', { name: 'Workspace providers' });
			const first = region.getByRole('button', { name: 'Personal Max', exact: true });
			const second = region.getByRole('button', { name: 'Acme (Team)', exact: true });
			await expect(first).toBeEnabled();
			await expect(second).toBeEnabled();
			await first.click();
			await expect.poll(() => saves).toBe(1);
			await expect(second).toBeEnabled();
			await second.click();
			await expect.poll(() => saves).toBe(2);
			await expect(second).toBeEnabled();
			await page.screenshot({ path: testInfo.outputPath(`post-second-toggle-${surface}.png`), animations: 'disabled', scale: 'css' });
		}
	});
});
