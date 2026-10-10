import { expect, test } from '@playwright/test';
import { loginByUrl, openSettings, TEST_TOKEN } from '../support/corsia';

const SHOTS = process.env.PICKER_SHOTS_DIR;
const capsule = { context_window_max: 200000, supports_vision: true, supports_tools: true, supports_reasoning: true, reasoning_config: null };
const claudeAccounts = [
	['personal', 'Personal Max', 62, 41],
	['team', 'Acme Team', 42, 78],
	['studio', 'Studio', 15, 25],
].map(([id, name, fiveHour, weekly]) => ({
	id, provider: 'claude-code', name, is_default: id === 'personal', state: 'signed_in', revision: 1, identity: {},
	label: { name, email: `${id}@example.test`, text: `${name} · ${id}@example.test`, custom: null },
	usage: { state: 'allowed', usable: true, checking: false, check: null, windows: [
		{ kind: 'five_hour', state: 'reported', used_percent: fiveHour, window_minutes: 300 },
		{ kind: 'seven_day', state: 'reported', used_percent: weekly, window_minutes: 10080 },
	] },
}));
const runsOn = claudeAccounts.map((account, index) => ({ account_id: account.id, selected: index === 0, status: 'ready', status_reason: null, resets_at: null, usage: account.usage }));
const catalog = [
	{ id: 'claude-code/opus', selector: 'claude-code/opus', litellm_model: 'claude-code/opus', name: 'Claude Code · Opus 5.5', display_name: 'Opus 5.5', connection: 'claude-code', provider: 'claude-code', backend: 'cc', status: 'ready', available: true, desc: '', size: '', speed: '', capabilities: capsule, runs_on: runsOn },
	{ id: 'claude-code/sonnet', selector: 'claude-code/sonnet', litellm_model: 'claude-code/sonnet', name: 'Claude Code · Sonnet 5.5', display_name: 'Sonnet 5.5', connection: 'claude-code', provider: 'claude-code', backend: 'cc', status: 'ready', available: true, desc: '', size: '', speed: '', capabilities: capsule, runs_on: runsOn },
	{ id: 'claude-code/haiku', selector: 'claude-code/haiku', litellm_model: 'claude-code/haiku', name: 'Claude Code · Haiku 4.5', display_name: 'Haiku 4.5', connection: 'claude-code', provider: 'claude-code', backend: 'cc', status: 'ready', available: true, desc: '', size: '', speed: '', capabilities: capsule, runs_on: runsOn },
	{ id: 'claude-code/claude-sonnet-5-5', selector: 'claude-code/claude-sonnet-5-5', litellm_model: 'claude-code/claude-sonnet-5-5', name: 'Claude Code · Sonnet 5.5', display_name: 'Sonnet 5.5', connection: 'claude-code', provider: 'claude-code', backend: 'cc', status: 'unavailable', status_reason: 'not_returned_by_discovery', available: false, desc: '', size: '', speed: '', capabilities: capsule },
];
const settingsRows = catalog.map((row) => ({ ...row, harness: 'claude-code', enabled: 1, provider_model_id: row.id.split('/').at(-1), context_window: 200000 }));

async function shot(page: import('@playwright/test').Page, name: string) {
	if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}-${page.viewportSize()!.width}x${page.viewportSize()!.height}.png`, fullPage: true, animations: 'disabled' });
}

test('model names render from the catalog projection', async ({ page }) => {
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.setViewportSize({ width: 390, height: 844 });
	await page.route('**/api/models**', async (route) => route.request().method() === 'GET'
		? route.fulfill({ json: { models: catalog, active_local_threads: 0 } }) : route.continue());
	await page.route('**/api/settings/models**', async (route) => route.request().method() === 'GET'
		? route.fulfill({ json: { models: settingsRows } }) : route.continue());
	await page.route('**/api/vendor-accounts**', async (route) => route.request().method() === 'GET'
		? route.fulfill({ json: { accounts: claudeAccounts } }) : route.continue());
	await loginByUrl(page);
	await page.locator('button.model-chip').click();
	await expect(page.locator('.model-sheet')).toContainText('Opus 5.5');
	await expect(page.locator('.model-sheet')).toContainText('Haiku 4.5');
	await expect(page.locator('.model-sheet')).not.toContainText('not returned');
	for (const account of claudeAccounts) await expect(page.locator('.model-sheet')).toContainText(account.label.name);
	for (const segment of ['5h 38% left', 'wk 59% left', '5h 58% left', 'wk 22% left', '5h 85% left', 'wk 75% left']) {
		await expect(page.locator('.model-sheet')).toContainText(segment);
	}
	for (const theme of ['light', 'dark']) {
		await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
		await shot(page, `picker-${theme}`);
	}
	if ((page.viewportSize()?.width ?? 0) > 768) {
		await openSettings(page);
		await page.getByRole('button', { name: 'Providers' }).click();
		await expect(page.getByText('Claude Code', { exact: true }).first()).toBeVisible();
		await page.getByText('Claude Code', { exact: true }).first().click();
		await expect(page.getByText('Opus 5.5', { exact: true }).first()).toBeVisible();
		await expect(page.getByText('Haiku 4.5', { exact: true }).first()).toBeVisible();
		await shot(page, 'settings-claude-expanded');
	}
});

const parityCatalog = [
	// Source-derived paths: visible rows, aliases, unavailable discovery, and Settings-only rows.
	{ id: 'claude-code/opus', selector: 'claude-code/opus', litellm_model: 'claude-code/opus', name: 'Claude Code · Opus 5.5', display_name: 'Opus 5.5', connection: 'claude-code', provider: 'claude-code', backend: 'cc', status: 'ready', available: true, desc: '', size: '', speed: '', capabilities: capsule },
	{ id: 'claude-code/sonnet', selector: 'claude-code/sonnet', litellm_model: 'claude-code/sonnet', name: 'Claude Code · Sonnet 5.5', display_name: 'Sonnet 5.5', connection: 'claude-code', provider: 'claude-code', backend: 'cc', status: 'ready', available: true, desc: '', size: '', speed: '', capabilities: capsule },
	{ id: 'claude-code/sonnet[1m]', selector: 'claude-code/sonnet[1m]', litellm_model: 'claude-code/sonnet[1m]', name: 'Claude Code · Sonnet 5.5 (1M)', display_name: 'Sonnet 5.5 (1M)', connection: 'claude-code', provider: 'claude-code', backend: 'cc', status: 'ready', available: true, alias_of: 'claude-code/sonnet', desc: '', size: '', speed: '', capabilities: capsule },
	{ id: 'claude-code/retired', selector: 'claude-code/retired', litellm_model: 'claude-code/retired', name: 'Claude Code · Retired', display_name: 'Retired', connection: 'claude-code', provider: 'claude-code', backend: 'cc', status: 'unavailable', available: false, desc: '', size: '', speed: '', capabilities: capsule },
	{ id: 'codex/gpt-6.1-sol', selector: 'codex/gpt-6.1-sol', litellm_model: 'codex/gpt-6.1-sol', name: 'Codex · GPT-6.1 Sol', display_name: 'GPT-6.1 Sol', connection: 'codex', provider: 'codex', backend: 'codex', status: 'ready', available: true, desc: '', size: '', speed: '', capabilities: capsule },
	{ id: 'codex/gpt-5.6-terra', selector: 'codex/gpt-5.6-terra', litellm_model: 'codex/gpt-5.6-terra', name: 'Codex · GPT-5.6 Terra', display_name: 'GPT-5.6 Terra', connection: 'codex', provider: 'codex', backend: 'codex', status: 'ready', available: true, desc: '', size: '', speed: '', capabilities: capsule },
	{ id: 'codex/gpt-5.6-terra[1m]', selector: 'codex/gpt-5.6-terra[1m]', litellm_model: 'codex/gpt-5.6-terra[1m]', name: 'Codex · GPT-5.6 Terra (1M)', display_name: 'GPT-5.6 Terra (1M)', connection: 'codex', provider: 'codex', backend: 'codex', status: 'ready', available: true, alias_of: 'codex/gpt-5.6-terra', desc: '', size: '', speed: '', capabilities: capsule },
];
const paritySettingsRows = [
	...parityCatalog.map((row) => ({ ...row, harness: row.connection, enabled: 1, provider_model_id: row.id.split('/').at(-1), context_window: 200000 })),
	{ id: 'claude-code/reference', harness: 'claude-code', provider: 'claude-code', display_name: 'Reference only', source: 'reference', enabled: 0, available: 0 },
	{ id: 'codex/hidden', harness: 'codex', provider: 'codex', display_name: 'Hidden only', enabled: 0, available: 1 },
];
const pickerExpected = {
	'Claude Code': ['Opus 5.5', 'Sonnet 5.5'],
	Codex: ['GPT-6.1 Sol', 'GPT-5.6 Terra'],
};
const settingsExpected = {
	...pickerExpected,
	Codex: [...pickerExpected.Codex, 'Hidden only'],
};

test('Settings retains hidden discovered harness rows while the picker shows its final membership', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'This proof sets both requested viewports itself.');
	await page.route('**/api/models**', async (route) => route.request().method() === 'GET'
		? route.fulfill({ json: { models: parityCatalog, active_local_threads: 0 } }) : route.continue());
	await page.route('**/api/settings/models**', async (route) => route.request().method() === 'GET'
		? route.fulfill({ json: { models: paritySettingsRows } }) : route.continue());
	await page.route('**/api/vendor-accounts**', async (route) => route.request().method() === 'GET'
		? route.fulfill({ json: { accounts: claudeAccounts } }) : route.continue());

	for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
		for (const theme of ['light', 'dark']) {
			await page.setViewportSize(viewport);
			await loginByUrl(page);
			await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
			await page.locator('button.model-chip').click();
			await expect(page.locator('.model-sheet')).toBeVisible();
			const pickerNames: Record<string, string[]> = {};
			for (const [label, expected] of Object.entries(pickerExpected)) {
				const group = page.locator(`section[aria-label="${label}"]`);
				await expect(group).toBeVisible();
				pickerNames[label] = await group.locator('.model-list-name').allTextContents();
				expect(pickerNames[label]).toEqual(expected);
			}
			await shot(page, `parity-picker-${theme}`);
			await page.keyboard.press('Escape');

			await openSettings(page);
			await page.getByRole('button', { name: 'Providers' }).click();
			const settingsNames: Record<string, string[]> = {};
			for (const [label, expected] of Object.entries(settingsExpected)) {
				const card = page.locator(`[data-provider-card="${label === 'Claude Code' ? 'claude-code' : 'codex'}"]`);
				await expect(card.locator('.pr-head')).toBeVisible();
				await card.locator('.pr-head').click();
				await expect(card.locator('.mname').first()).toBeVisible();
				settingsNames[label] = await card.locator('.mname').allTextContents();
				expect(settingsNames[label]).toEqual(expected);
				if (label === 'Claude Code') expect(settingsNames[label]).toEqual(pickerNames[label]);
			}
			await expect(page.locator('.model-sheet')).toHaveCount(0);
			await shot(page, `parity-settings-${theme}`);
		}
	}
});

test('a discovered harness model can be hidden and re-enabled into the picker', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'This interaction proof is desktop-only.');
	let enabled = true;
	const target = {
		id: 'codex/hidden', selector: 'codex/hidden', litellm_model: 'codex/hidden',
		name: 'Codex · Hidden only', display_name: 'Hidden only', connection: 'codex', provider: 'codex',
		backend: 'codex', status: 'ready', available: true, desc: '', size: '', speed: '', capabilities: capsule,
	};
	const initialSettingsRows = [
		...paritySettingsRows.filter((row) => row.id !== target.id),
		{ ...target, harness: 'codex', enabled: 1, provider_model_id: 'hidden', context_window: 200000 },
	];
	await page.route('**/api/models**', async (route) => route.request().method() === 'GET'
		? route.fulfill({ json: { models: enabled ? [...parityCatalog, target] : parityCatalog, active_local_threads: 0 } }) : route.continue());
	await page.route('**/api/settings/models**', async (route) => {
		if (route.request().method() === 'GET') {
			return route.fulfill({ json: { models: initialSettingsRows.map((row) => row.id === target.id ? { ...row, enabled: enabled ? 1 : 0 } : row) } });
		}
		if (route.request().method() === 'PATCH') {
			const patch = route.request().postDataJSON() as { enabled?: boolean };
			expect(patch).toEqual({ enabled: !enabled });
			enabled = Boolean(patch.enabled);
			return route.fulfill({ json: { ok: true } });
		}
		return route.continue();
	});
	await page.route('**/api/vendor-accounts**', async (route) => route.request().method() === 'GET'
		? route.fulfill({ json: { accounts: claudeAccounts } }) : route.continue());

	await loginByUrl(page);
	await page.locator('button.model-chip').click();
	await expect(page.locator('section[aria-label="Codex"]').getByText('Hidden only', { exact: true })).toBeVisible();
	await page.keyboard.press('Escape');
	await openSettings(page);
	await page.getByRole('button', { name: 'Providers' }).click();
	const card = page.locator('[data-provider-card="codex"]');
	await card.locator('.pr-head').click();
	const targetRow = card.locator('.mr', { hasText: 'Hidden only' });
	await expect(targetRow.getByRole('button', { name: 'Hide from picker' })).toBeVisible();
	await targetRow.getByRole('button', { name: 'Hide from picker' }).click();
	await expect(targetRow.getByRole('button', { name: 'Show in picker' })).toBeVisible();
	await targetRow.getByRole('button', { name: 'Show in picker' }).click();
	await expect(targetRow.getByRole('button', { name: 'Hide from picker' })).toBeVisible();

	await loginByUrl(page);
	await page.locator('button.model-chip').click();
	await expect(page.locator('section[aria-label="Codex"]').getByText('Hidden only', { exact: true })).toBeVisible();
});
