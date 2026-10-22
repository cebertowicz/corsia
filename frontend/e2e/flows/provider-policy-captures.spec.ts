import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';
import { loginByUrl, openSettingsSection } from '../support/corsia';

type ScreenId = 'W1' | 'R1' | 'R2' | 'R3' | 'R4' | 'R5';

const SCREENS: readonly ScreenId[] = ['W1', 'R1', 'R2', 'R3', 'R4', 'R5'];

function surfacesFor(projectName: string) {
	if (projectName === 'chromium-desktop') {
		return [
			['desktop-light', 1920, 1080, 'light'],
			['desktop-dark', 1920, 1080, 'dark'],
		] as const;
	}
	return [
		['phone-light', 393, 852, 'light'],
		['phone-dark', 393, 852, 'dark'],
	] as const;
}

function providerPageSurfaces(projectName: string) {
	if (projectName === 'chromium-desktop') {
		return [
			['desktop-light', 1440, 1080, 'light'],
			['desktop-dark', 1440, 1080, 'dark'],
		] as const;
	}
	return [
		['phone-light', 393, 852, 'light'],
		['phone-dark', 393, 852, 'dark'],
	] as const;
}

// Project-policy error states get the same review width as the decisive page pair.
const projectPolicySurfaces = providerPageSurfaces;

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

async function serveHarnessCatalog(page: Page) {
	// The decisive before/after pair uses this named seed: both harnesses and all four accounts.
	const models = [
		{ id: 'claude-code/sonnet', connection: 'claude-code', provider: 'claude-code', status: 'ready', available: true, backend: 'network' },
		{ id: 'codex/gpt-5', connection: 'codex', provider: 'codex', status: 'ready', available: true, backend: 'network' },
	];
	await page.route('**/api/settings/models**', (route) => route.fulfill({ json: { models: models.map((model) => ({ ...model, enabled: 1 })) } }));
	await page.route('**/api/models**', (route) => route.fulfill({ json: { models } }));
}

async function servePolicyStates(page: Page, state: { screen: ScreenId }) {
	await serveHarnessCatalog(page);
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

async function waitForModalToSettle(modal: Locator) {
	await expect.poll(async () => modal.evaluate((element) => element.getAnimations({ subtree: true }).length)).toBe(0);
	await expect(modal).toBeVisible();
}

async function openWorkspaceProviderAccounts(page: Page) {
	const card = page.locator('[data-provider-card="claude-code"]');
	await card.getByRole('button', { name: 'Settings' }).click();
	const modal = page.locator('.modal');
	await expect(modal.getByRole('button', { name: 'Accounts', exact: true })).toBeVisible();
	await expect(modal.locator('.accounts')).toBeVisible();
	await waitForModalToSettle(modal);
	return modal;
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
	await expect(page.getByRole('region', { name: 'Identity' }).getByRole('textbox', { name: 'Name', exact: true })).toHaveValue(name);
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
					const accounts = await openWorkspaceProviderAccounts(page);
					await expect(accounts.getByText('Overridden in 2 projects:')).toBeVisible();
					await expect(accounts.getByRole('button', { name: 'Atlas migration' })).toBeVisible();
					await expect(accounts.getByRole('button', { name: 'Field notes' })).toBeVisible();
					await expect(accounts.getByText('Personal Max · sam@example.com')).toBeVisible();
					await expect(accounts.getByLabel('Account usage').first()).toHaveText('5h 38% left · wk 59% left');
					await accounts.scrollIntoViewIfNeeded();
					await expect(accounts.getByRole('button', { name: 'Move Personal Max · sam@example.com up' })).toBeVisible();
				} else {
					await openActivity(page);
					await createProjectAndOpenSettings(page, `subscription-${screen}-${surface}`);
					const region = page.getByRole('region', { name: 'Project providers' });
					await expect(region).toBeVisible();
					if (screen === 'R1') await expect(region.getByRole('button', { name: 'Use workspace settings' })).toHaveAttribute('aria-pressed', 'true');
					if (screen === 'R2') {
						await expect(region.getByText("Turning this on replaces this project's settings with the workspace's.")).toBeVisible();
						await expect(region.getByRole('button', { name: 'Open workspace providers' })).toBeVisible();
					}
					if (screen === 'R3') await expect(region.getByRole('button', { name: 'Use Personal Max', exact: true })).toHaveAttribute('aria-pressed', 'false');
					if (screen === 'R4') await expect(region.getByRole('button', { name: 'Sign in' })).toBeVisible();
					await region.evaluate((element) => element.scrollIntoView({ block: 'start' }));
					if (screen === 'R5') {
						await expect(region.getByRole('button', { name: 'Use Acme (Team)' })).toBeVisible();
						await expect(region.getByRole('button', { name: 'Use Pro', exact: true })).toBeVisible();
						await expect(region.getByRole('button', { name: 'Roll over when an account hits its limit' })).toHaveCount(0);
						await expect(region.getByRole('button', { name: /Move / })).toHaveCount(0);
					}
				}
				await page.screenshot({ path: testInfo.outputPath(`${screen}-${surface}.png`), animations: 'disabled', scale: 'css' });
				if (screen === 'R4') {
					await page.getByRole('button', { name: 'Sign in' }).click();
					await expect(page.locator('.settings-content')).toBeVisible();
				}
			}
		}
	});

	test('captures the workspace Providers page after removing its duplicate policy section', async ({ page }, testInfo) => {
		test.setTimeout(120_000);
		const state: { screen: ScreenId } = { screen: 'W1' };
		await servePolicyStates(page, state);
		for (const [surface, width, height, colorScheme] of providerPageSurfaces(testInfo.project.name)) {
			await page.setViewportSize({ width, height });
			await page.emulateMedia({ colorScheme });
			await loginByUrl(page, { waitForComposer: false });
			await openActivity(page);
			await page.getByRole('button', { name: 'Workspace settings' }).click();
			await openSettingsSection(page, 'Providers');
			const content = page.locator('.settings-content');
			await content.evaluate((element) => { element.scrollTop = element.scrollHeight; });
			await expect(page.locator('.modal')).toHaveCount(0);
			await expect(content.locator('[data-provider-card="claude-code"]')).toBeVisible();
			await expect(content.locator('[data-provider-card="codex"]')).toBeVisible();
			await expect(content.getByText('Accounts and rollover', { exact: true })).toHaveCount(0);
			await page.screenshot({ path: testInfo.outputPath(`workspace-providers-bottom-${surface}.png`), animations: 'disabled', scale: 'css' });
		}
	});

	test('keeps two account toggles usable after each policy save', async ({ page }, testInfo) => {
		test.setTimeout(120_000);
		let policy = policyFor('W1');
		let saves = 0;
		await serveHarnessCatalog(page);
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
			const accounts = await openWorkspaceProviderAccounts(page);
			const first = accounts.getByRole('button', { name: 'Use Personal Max · sam@example.com', exact: true });
			const second = accounts.getByRole('button', { name: 'Use Acme (Team) · sam@example.com', exact: true });
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

	test('captures long-name, usage, managed, error, and pending account-policy states', async ({ page }, testInfo) => {
		test.setTimeout(120_000);
		let accounts = vendorAccounts();
		let policy = policyFor('W1');
		let saveAttempt = 0;
		let releaseBusy: (() => void) | null = null;
		await serveHarnessCatalog(page);
		await page.route('**/api/vendor-accounts**', async (route) => {
			if (route.request().method() === 'GET') await route.fulfill({ json: { accounts } });
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
				saveAttempt += 1;
				if (saveAttempt % 2 === 1) {
					await route.fulfill({ status: 500, json: { detail: 'Could not save subscriptions.' } });
					return;
				}
				await new Promise<void>((resolve) => { releaseBusy = resolve; });
				const provider = policy.providers.find((entry) => pathname.endsWith(entry.provider_key));
				await route.fulfill({ json: provider ?? policy });
				return;
			}
			await route.continue();
		});

		async function openWorkspace(surface: string) {
			await loginByUrl(page, { waitForComposer: false });
			await openActivity(page);
			await page.getByRole('button', { name: 'Workspace settings' }).click();
			await openSettingsSection(page, 'Providers');
			return openWorkspaceProviderAccounts(page);
		}

		for (const [surface, width, height, colorScheme] of surfacesFor(testInfo.project.name)) {
			await page.setViewportSize({ width, height });
			await page.emulateMedia({ colorScheme });

			accounts = vendorAccounts().map((account, index) => index === 0
				? { ...account, name: 'Personal account with an intentionally long display name for narrow layouts', label: { ...account.label, name: 'Personal account with an intentionally long display name for narrow layouts', text: 'Personal account with an intentionally long display name for narrow layouts · sam@example.com' } }
				: account);
			policy = { ...policyFor('W1'), overriding_projects: ['Atlas migration', 'Field notes', 'Release operations', 'Research notebook', 'Customer support'].map((name, index) => ({ id: `project-${index}`, name })) };
			let modal = await openWorkspace(surface);
			await expect(modal.getByText('Overridden in 5 projects:')).toBeVisible();
			await expect(modal.getByRole('button', { name: 'Move Personal account with an intentionally long display name for narrow layouts · sam@example.com up' })).toBeVisible();
			await page.screenshot({ path: testInfo.outputPath(`long-name-five-projects-${surface}.png`), animations: 'disabled', scale: 'css' });

			accounts = vendorAccounts();
			policy = { ...policyFor('W1'), providers: policyFor('W1').providers.map((provider) => provider.provider_key === 'claude-code' ? { ...provider, rollover: false, order: null } : provider) };
			modal = await openWorkspace(surface);
			await expect(modal.getByRole('button', { name: 'Roll over when an account hits its limit' })).toBeVisible();
			await expect(modal.getByRole('button', { name: /Move Personal Max/ })).toHaveCount(0);
			await expect(modal.getByRole('button', { name: 'Use Personal Max · sam@example.com', exact: true })).toBeVisible();
			await page.screenshot({ path: testInfo.outputPath(`rollover-off-${surface}.png`), animations: 'disabled', scale: 'css' });

			accounts = vendorAccounts().map((account, index) => index === 0
				? { ...account, usage: { ...account.usage, state: 'limited', usable: false, resets_at: '2026-10-19T00:00:00Z' } }
				: index === 1 ? { ...account, usage: { ...account.usage, windows: [{ kind: 'five_hour', state: 'unknown', used_percent: null, window_minutes: 300 }] } } : account);
			policy = policyFor('W1');
			modal = await openWorkspace(surface);
			await expect(modal.getByText(/Usage limit reached/).first()).toBeVisible();
			await expect(modal.getByText(/usage unknown/i)).toBeVisible();
			await page.screenshot({ path: testInfo.outputPath(`limited-unknown-${surface}.png`), animations: 'disabled', scale: 'css' });

			accounts = vendorAccounts().map((account, index) => index === 0 ? { ...account, state: 'legacy' } : account);
			policy = policyFor('W1');
			modal = await openWorkspace(surface);
			await expect(modal.getByText('Managed by provider')).toBeVisible();
			await expect(modal.getByRole('button', { name: 'Roll over when an account hits its limit' })).toBeVisible();
			await expect(modal.getByRole('button', { name: 'Move Personal Max · sam@example.com up' })).toBeVisible();
			await expect(modal.getByRole('button', { name: 'Use Personal Max · sam@example.com', exact: true })).toBeVisible();
			await page.screenshot({ path: testInfo.outputPath(`managed-${surface}.png`), animations: 'disabled', scale: 'css' });

			accounts = vendorAccounts();
			saveAttempt = 0;
			modal = await openWorkspace(surface);
			const savedToggle = modal.getByRole('button', { name: 'Use Personal Max · sam@example.com', exact: true });
			await savedToggle.click();
			await expect(modal.getByText("Couldn’t save this change. Could not save subscriptions.")).toBeVisible();
			await expect(savedToggle).toHaveAttribute('aria-pressed', 'true');
			await page.screenshot({ path: testInfo.outputPath(`save-error-${surface}.png`), animations: 'disabled', scale: 'css' });

			modal = await openWorkspace(surface);
			const toggle = modal.getByRole('button', { name: 'Use Personal Max · sam@example.com', exact: true });
			const otherToggle = modal.getByRole('button', { name: 'Use Acme (Team) · sam@example.com', exact: true });
			const rolloverToggle = modal.getByRole('button', { name: 'Roll over when an account hits its limit' });
			await toggle.click();
			await expect(toggle).toBeDisabled();
			await expect(modal.getByText('Saving…')).toBeVisible();
			await expect(otherToggle).toBeEnabled();
			await expect(rolloverToggle).toBeEnabled();
			await page.screenshot({ path: testInfo.outputPath(`busy-${surface}.png`), animations: 'disabled', scale: 'css' });
			releaseBusy?.();
			await expect(toggle).toBeEnabled();

			// The rollover switch shares the account rows' status treatment: odd saves fail, even saves hold.
			modal = await openWorkspace(surface);
			const failedRollover = modal.getByRole('button', { name: 'Roll over when an account hits its limit' });
			await failedRollover.click();
			await expect(modal.getByText("Couldn’t save this change. Could not save subscriptions.")).toBeVisible();
			await page.screenshot({ path: testInfo.outputPath(`rollover-save-error-${surface}.png`), animations: 'disabled', scale: 'css' });

			modal = await openWorkspace(surface);
			const busyRollover = modal.getByRole('button', { name: 'Roll over when an account hits its limit' });
			await busyRollover.click();
			await expect(busyRollover).toBeDisabled();
			await expect(modal.getByText('Saving…')).toBeVisible();
			await page.screenshot({ path: testInfo.outputPath(`rollover-busy-${surface}.png`), animations: 'disabled', scale: 'css' });
			releaseBusy?.();
			await expect(busyRollover).toBeEnabled();
		}
	});

	test('captures project override account and scope save states', async ({ page }, testInfo) => {
		test.setTimeout(240_000);
		let policy = policyFor('R2');
		let failNext = false;
		let release: (() => void) | null = null;
		await serveHarnessCatalog(page);
		await page.route('**/api/vendor-accounts**', (route) => route.request().method() === 'GET'
			? route.fulfill({ json: { accounts: vendorAccounts() } }) : route.continue());
		await page.route('**/api/provider-policy**', async (route) => {
			const request = route.request();
			if (request.method() === 'GET') { await route.fulfill({ json: policy }); return; }
			if (request.method() === 'PUT') {
				if (failNext) { await route.fulfill({ status: 500, json: { detail: 'Could not save subscriptions.' } }); return; }
				await new Promise<void>((resolve) => { release = resolve; });
				const provider = policy.providers.find((item) => request.url().includes(item.provider_key));
				await route.fulfill({ json: provider ?? policy });
				return;
			}
			await route.continue();
		});
		await page.route('**/api/projects/*/provider-policy/use-workspace', async (route) => {
			if (failNext) { await route.fulfill({ status: 500, json: { detail: 'Could not save subscriptions.' } }); return; }
			await new Promise<void>((resolve) => { release = resolve; });
			await route.fulfill({ json: policy });
		});

		async function openProject(surface: string, state: string) {
			await loginByUrl(page, { waitForComposer: false });
			await openActivity(page);
			await createProjectAndOpenSettings(page, `project-policy-${state}-${surface}`);
			const region = page.getByRole('region', { name: 'Project providers' });
			await expect(region).toBeVisible();
			// toBeVisible passes below the fold; the frame must show the policy rows themselves.
			await region.evaluate((element) => element.scrollIntoView({ block: 'start' }));
			return region;
		}
		for (const [surface, width, height, colorScheme] of projectPolicySurfaces(testInfo.project.name)) {
			await page.setViewportSize({ width, height });
			await page.emulateMedia({ colorScheme });
			policy = policyFor('R2');

			failNext = true;
			let region = await openProject(surface, 'account-error');
			await region.getByRole('button', { name: 'Use Personal Max', exact: true }).click();
			await expect(region.getByText("Couldn’t save this change. Could not save subscriptions.")).toBeVisible();
			await page.screenshot({ path: testInfo.outputPath(`project-account-save-error-${surface}.png`), animations: 'disabled', scale: 'css' });

			failNext = false;
			release = null;
			region = await openProject(surface, 'account-busy');
			const accountToggle = region.getByRole('button', { name: 'Use Personal Max', exact: true });
			await accountToggle.click();
			await expect(accountToggle).toBeDisabled();
			await expect(region.getByText('Saving…')).toBeVisible();
			await page.screenshot({ path: testInfo.outputPath(`project-account-busy-${surface}.png`), animations: 'disabled', scale: 'css' });
			release?.();
			await expect(accountToggle).toBeEnabled();

			failNext = true;
			region = await openProject(surface, 'scope-error');
			await region.getByRole('button', { name: 'Use workspace settings', exact: true }).click();
			await expect(region.getByText("Couldn’t save this change. Could not save subscriptions.")).toBeVisible();
			await page.screenshot({ path: testInfo.outputPath(`project-scope-save-error-${surface}.png`), animations: 'disabled', scale: 'css' });

			failNext = false;
			release = null;
			region = await openProject(surface, 'scope-busy');
			const scopeToggle = region.getByRole('button', { name: 'Use workspace settings', exact: true });
			await scopeToggle.click();
			await expect(scopeToggle).toBeDisabled();
			await expect(region.getByText('Saving…')).toBeVisible();
			await page.screenshot({ path: testInfo.outputPath(`project-scope-busy-${surface}.png`), animations: 'disabled', scale: 'css' });
			release?.();
			await expect(scopeToggle).toBeEnabled();
		}
	});
});
