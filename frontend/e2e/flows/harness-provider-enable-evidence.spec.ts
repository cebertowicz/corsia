import { expect, test, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chatComposer, TEST_TOKEN } from '../support/corsia';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const evidenceDir = path.join(repoRoot, '.qa/harness-provider-enable');
const AUThed = (route: string) => `${route}?token=${encodeURIComponent(TEST_TOKEN)}`;

const capabilities = {
	context_window_max: 262144,
	supports_vision: true,
	supports_tools: true,
	supports_reasoning: true,
	reasoning_config: { type: 'effort_levels', values: ['low', 'medium', 'high'], default: 'medium' },
};

const claudeModel = {
	id: 'claude-code/sonnet', selector: 'claude-code/sonnet', litellm_model: 'claude-code/sonnet',
	name: 'Claude Code · Sonnet 5.5', display_name: 'Sonnet 5.5', connection: 'claude-code',
	provider: 'claude-code', backend: 'cc', desc: '', size: '', speed: '', status: 'ready',
	available: true, capabilities, runs_on: [],
};

const settingsModels = [
	{ ...claudeModel, harness: 'claude-code', enabled: true, provider_enabled: true, source: 'fixture' },
	{
		id: 'codex/gpt-5.6-terra', selector: 'codex/gpt-5.6-terra', litellm_model: 'codex/gpt-5.6-terra',
		name: 'Codex · GPT-5.6 Terra', display_name: 'GPT-5.6 Terra', connection: 'codex', provider: 'codex',
		harness: 'codex', backend: 'codex', available: true, enabled: 0, provider_enabled: false, source: 'fixture',
	},
];

const accounts = [
	{ id: 'claude-fixture', provider: 'claude-code', name: 'Claude fixture', is_default: true, state: 'signed_in', revision: 1, identity: {}, label: { name: 'Claude fixture', email: null, text: 'Claude fixture', custom: null } },
	{ id: 'codex-fixture', provider: 'codex', name: 'Codex fixture', is_default: true, state: 'signed_in', revision: 1, identity: {}, label: { name: 'Codex fixture', email: null, text: 'Codex fixture', custom: null } },
];

const providers = [
	{ name: 'external', label: 'External', type: 'cloud', has_key: true, masked_key: null, api_base: null, docs_url: null, models: ['fixture-cloud'], enabled: true },
];

function surface(testInfo: TestInfo): 'desktop' | '390px' {
	return testInfo.project.name === 'chromium-mobile' ? '390px' : 'desktop';
}

async function useEvidenceViewport(page: Page, testInfo: TestInfo) {
	if (surface(testInfo) === '390px') await page.setViewportSize({ width: 390, height: 844 });
}

async function fixtureRoutes(page: Page) {
	await page.route('**/api/models**', async (route) => {
		if (route.request().method() === 'GET') await route.fulfill({ json: { models: [claudeModel], active_local_threads: 0 } });
		else await route.continue();
	});
	await page.route('**/api/settings/models**', async (route) => {
		if (route.request().method() === 'GET') await route.fulfill({ json: { models: settingsModels } });
		else await route.continue();
	});
	await page.route('**/api/vendor-accounts**', async (route) => {
		if (route.request().method() === 'GET') await route.fulfill({ json: { accounts } });
		else await route.continue();
	});
	await page.route('**/api/settings/providers**', async (route) => {
		if (route.request().method() === 'GET') await route.fulfill({ json: { providers } });
		else await route.continue();
	});
	await page.route('**/api/settings/network-servers**', async (route) => {
		if (route.request().method() === 'GET') await route.fulfill({ json: { servers: [] } });
		else await route.continue();
	});
	await page.route('**/api/system/host-resources**', async (route) => {
		if (route.request().method() === 'GET') await route.fulfill({ json: { device_name: 'Fixture Mac', total_gb: 1000, free_gb: 700, models_used_gb: 0, other_used_gb: 0, safe_free_gb: 100, safe_reserve_gb: 100, by_provider: {} } });
		else await route.continue();
	});
}

function seedHeldCodexInput(threadId: string): void {
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !workspace || !stateRoot) {
		throw new Error('The harness evidence fixture requires isolated e2e-local mock state.');
	}
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', `
import sys
from connections.harness_models import store_success
from connections.models import ConnectionStatus, ConnectionType, set_harness_enabled, upsert_connection
from infra.input_writes import release_batch_before_write
from infra.send_target import CODEX_PROVIDER_DISABLED
from infra.thread_driver import admit_input, finish_turn
from infra.time import utc_now_iso

thread_id = sys.argv[1]
upsert_connection(type=ConnectionType.CLI, provider='codex', status=ConnectionStatus.AVAILABLE, config={'binary': '/bin/codex'})
store_success('codex', [{'selector_id': 'gpt-5.6-terra', 'display_name': 'GPT-5.6 Terra', 'description': '', 'available': True, 'resolved_identity': 'gpt-5.6-terra', 'capabilities': {}, 'context_window': 262144, 'is_default': False}], harness_version='fixture', source='e2e-fixture', timestamp='2026-10-13T00:00:00+00:00')
set_harness_enabled('codex', False)
admission = admit_input(thread_id, role='user', content='Send this when Codex is back on.', intent='apply_now', message_id=thread_id + ':codex-disabled', turn_id=thread_id + ':codex-disabled-turn', boot_epoch='harness-e2e', admitted_at=utc_now_iso())
assert admission.batch is not None
release_batch_before_write(admission.batch, hold_error=CODEX_PROVIDER_DISABLED)
terminal = finish_turn(thread_id + ':codex-disabled-turn', outcome='error', ended_at=utc_now_iso(), successor_turn_id=thread_id + ':unused', boot_epoch='harness-e2e')
assert terminal.successor is None
`, threadId,
	], { cwd: repoRoot, encoding: 'utf8', env: {
		...process.env, PYTHONPATH: path.join(repoRoot, 'backend'), CORSIA_WORKSPACE: workspace,
		CORSIA_STATE_DIR: stateRoot, CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1',
	} });
}

async function createCodexThread(request: APIRequestContext, title: string): Promise<string> {
	const response = await request.post(AUThed('/api/threads'), { data: { title, model: 'codex/gpt-5.6-terra' } });
	expect(response.ok()).toBeTruthy();
	return (await response.json()).thread_id as string;
}

async function capture(page: Page, testInfo: TestInfo, name: string, theme: 'light' | 'dark') {
	await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
	fs.mkdirSync(evidenceDir, { recursive: true });
	await page.screenshot({ path: path.join(evidenceDir, `${name}-${surface(testInfo)}-${theme}.png`), scale: 'css', animations: 'disabled' });
}

for (const colorScheme of ['light', 'dark'] as const) {
	test.use({ colorScheme });

	test(`@surface:providers captures harness enable states in ${colorScheme}`, async ({ page }, testInfo) => {
		await useEvidenceViewport(page, testInfo);
		await page.addInitScript((theme) => localStorage.setItem('corsia_theme', theme), colorScheme);
		await fixtureRoutes(page);
		await page.goto(AUThed('/'));
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
		if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.locator('button[title="Settings"]').first().click();
		await page.getByRole('button', { name: 'Providers' }).click();
		await expect(page.getByRole('heading', { name: 'Providers', exact: true })).toBeVisible();
		await expect(page.locator('[data-provider-card="claude-code"]')).toContainText('Claude Code');
		await expect(page.locator('[data-provider-card="claude-code"] button[aria-label="Disable"]')).toBeVisible();
		await expect(page.locator('[data-provider-card="codex"] button[aria-label="Enable"]')).toBeVisible();
		await capture(page, testInfo, 'providers-claude-on-codex-off', colorScheme);
	});

	test(`@surface:chat captures the Codex disabled input hold in ${colorScheme}`, async ({ page, request }, testInfo) => {
		await useEvidenceViewport(page, testInfo);
		await page.addInitScript((theme) => localStorage.setItem('corsia_theme', theme), colorScheme);
		await fixtureRoutes(page);
		const title = `Codex disabled fixture ${Date.now()}`;
		const threadId = await createCodexThread(request, title);
		seedHeldCodexInput(threadId);
		await page.goto(AUThed('/'));
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
		if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.locator('.thread-item').filter({ hasText: title }).click();
		await expect(chatComposer(page)).toBeVisible();
		await expect(page.getByText('Send this when Codex is back on.', { exact: false }).first()).toBeVisible();
		await expect(page.getByText('Codex is turned off · Settings > Providers', { exact: true })).toBeVisible();
		await capture(page, testInfo, 'held-input-codex-off', colorScheme);
	});

	test(`@surface:model-picker excludes turned-off Codex models in ${colorScheme}`, async ({ page }, testInfo) => {
		await useEvidenceViewport(page, testInfo);
		await page.addInitScript((theme) => localStorage.setItem('corsia_theme', theme), colorScheme);
		await fixtureRoutes(page);
		await page.goto(AUThed('/'));
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
		if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByRole('button', { name: 'New chat' }).first().click();
		await expect(chatComposer(page)).toBeVisible();
		await page.locator('button.model-chip').click();
		await expect(page.locator('.model-sheet')).toBeVisible();
		await expect(page.locator('.model-sheet')).toContainText('Claude Code');
		await expect(page.locator('.model-sheet')).not.toContainText('Codex');
		await expect(page.locator('.model-sheet')).not.toContainText('GPT-5.6 Terra');
		await capture(page, testInfo, 'model-picker-codex-absent', colorScheme);
	});
}
