import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Locator, type Page } from '@playwright/test';
import { assistantMessages, loginByUrl, openSettings, openSettingsSection, sendChatMessage, waitForWsConnected } from '../support/corsia';
import { setFocusMode } from '../support/focusMode';
import { seedMockResponses } from '../support/mock';

/**
 * Review frames for the local-only controls (Cloud models off, Private machine, the cloud-off model sheet).
 * Every frame is the real app with the server answers faked by route interception; nothing signs in beyond the
 * disposable test token. Enabled only with LOCAL_ONLY_FRAMES_OUT (the output folder); LOCAL_ONLY_FRAMES_MODE=before
 * renders just the project page and Endpoints tab, for a checkout that does not have the change.
 */
const OUT = process.env.LOCAL_ONLY_FRAMES_OUT;
const BEFORE = process.env.LOCAL_ONLY_FRAMES_MODE === 'before';
const REFERENCE_ONLY = process.env.LOCAL_ONLY_FRAMES_MODE === 'reference';

const surfaces = (projectName: string) => projectName === 'chromium-desktop'
	? ([['1440', 1440, 900, 'light'], ['1440', 1440, 900, 'dark']] as const)
	: ([['390', 390, 844, 'light'], ['390', 390, 844, 'dark']] as const);

const capsule = { context_window_max: 200000, supports_vision: true, supports_tools: true, supports_reasoning: true, reasoning_config: null };
const modelRow = (over: Record<string, unknown>) => ({ desc: '', size: '', speed: '', capabilities: capsule, available: true, ...over });
const cloudRow = (id: string, name: string, over: Record<string, unknown> = {}) => modelRow({
	id: `claude-code/${id}`, selector: `claude-code/${id}`, litellm_model: `claude-code/${id}`, name: `Claude Code · ${name}`, display_name: name,
	connection: 'claude-code', provider: 'claude-code', backend: 'cc', status: 'ready', is_local: false, ...over,
});
const cloudOff = { status: 'unavailable', status_reason: 'project_cloud_off' };
const localRow = (id: string, name: string, serverName: string, over: Record<string, unknown> = {}) => modelRow({
	id: `omlx/${id}`, selector: `omlx@${id}`, litellm_model: `omlx@${id}`, name, display_name: name,
	connection: 'omlx', provider: 'omlx', backend: 'network', server_name: serverName, status: 'ready', is_local: true, ...over,
});
const catalogs = {
	plain: [cloudRow('opus', 'Opus 5.5'), cloudRow('sonnet', 'Sonnet 5.5'), localRow('qwen', 'Qwen3 8B', 'This Mac')],
	someLocal: [cloudRow('opus', 'Opus 5.5', cloudOff), cloudRow('sonnet', 'Sonnet 5.5', cloudOff),
		localRow('qwen', 'Qwen3 8B', 'This Mac'), localRow('llama', 'Llama 3.3 70B', 'Studio mini')],
	noneReady: [cloudRow('opus', 'Opus 5.5', cloudOff), cloudRow('sonnet', 'Sonnet 5.5', cloudOff)],
	remoteNotPrivate: [cloudRow('opus', 'Opus 5.5'), cloudRow('sonnet', 'Sonnet 5.5'),
		localRow('qwen', 'Qwen3 8B', 'This Mac'), localRow('llama', 'Llama 3.3 70B', 'Studio mini', { is_local: false })],
	localDown: [cloudRow('opus', 'Opus 5.5', cloudOff), cloudRow('sonnet', 'Sonnet 5.5', cloudOff),
		localRow('qwen', 'Qwen3 8B', 'This Mac', { status: 'unavailable', status_reason: 'connection_unavailable' })],
};

const endpoint = (id: string, host: string, kind: 'local' | 'lan') => ({ id, server_id: id, host, kind, last_ok_at: null, last_latency_ms: kind === 'lan' ? 4 : 1 });
const server = (id: string, over: Record<string, unknown>) => ({
	id, provider: 'omlx', instance_id: id, instance_name: id, kind: 'managed', port: 8000, status: 'online', source: 'auto',
	latency_ms: 4, memory_total_mb: null, memory_used_mb: null, gpu_info: null, active_host: null, api_base: null, mgmt_base: null,
	loaded_models: [], metadata: {}, on_this_mac: false, ...over,
});
const servers = {
	remoteOff: server('srv-studio', { instance_name: 'Studio mini', endpoints: [endpoint('e1', '192.168.1.20', 'lan')], active_host: '192.168.1.20', private: false }),
	remoteOn: server('srv-studio', { instance_name: 'Studio mini', endpoints: [endpoint('e1', '192.168.1.20', 'lan')], active_host: '192.168.1.20', private: true }),
	thisMac: server('srv-local', { instance_name: 'oMLX', on_this_mac: true, endpoints: [endpoint('e2', '127.0.0.1', 'local')], active_host: '127.0.0.1' }),
	remoteOffline: server('srv-studio', { instance_name: 'Studio mini', status: 'offline', latency_ms: null, endpoints: [{ ...endpoint('e1', '192.168.1.20', 'lan'), last_latency_ms: null }], active_host: null, private: true }),
	loopback: server('srv-lms', { provider: 'external', kind: 'external', instance_name: 'LM Studio', port: 1234, dialect: 'openai',
		endpoints: [endpoint('e3', '127.0.0.1', 'local')], active_host: '127.0.0.1', private: false, metadata: { base_url: 'http://127.0.0.1:1234/v1' } }),
};

const vendorAccounts = [['personal', 'claude-code', 'Personal Max'], ['pro', 'codex', 'Pro']].map(([id, provider, name]) => ({
	id, provider, name, is_default: false, state: 'signed_in', revision: 1, identity: {},
	label: { name, email: 'sam@example.com', text: `${name} · sam@example.com`, custom: null },
	usage: { state: 'allowed', usable: true, checking: false, check: null, windows: [
		{ kind: 'five_hour', state: 'reported', used_percent: 40, window_minutes: 300 },
		{ kind: 'seven_day', state: 'reported', used_percent: 30, window_minutes: 10080 },
	] },
}));
const policy = {
	scope: 'project', use_workspace: false, override_revision: 1,
	providers: vendorAccounts.map((account) => ({
		provider_key: account.provider, capabilities: { units: 'accounts', rollover: true }, rollover: account.provider === 'claude-code', order: null, disabled: [], revision: 1,
		units: [{ id: account.id, label: account.name, enabled: true }],
	})),
};

type State = { models: unknown[]; servers: unknown[]; cloudOff: boolean; failSave: boolean; failPrivate?: boolean; hold?: Promise<void> | null };

async function serve(page: Page, state: State) {
	await page.route('**/api/models**', (route) => route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/models'
		? route.fulfill({ json: { models: state.models, active_local_threads: 0 } }) : route.continue());
	await page.route('**/api/vendor-accounts**', (route) => route.request().method() === 'GET' ? route.fulfill({ json: { accounts: vendorAccounts } }) : route.continue());
	await page.route('**/api/provider-policy**', (route) => route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/provider-policy'
		? route.fulfill({ json: policy }) : route.continue());
	await page.route('**/api/projects/*/cloud-models-off**', async (route) => {
		if (route.request().method() === 'PUT') {
			if (state.hold) await state.hold;
			if (state.failSave) return route.fulfill({ status: 500, json: { detail: 'Server unavailable' } });
			state.cloudOff = (route.request().postDataJSON() as { cloud_models_off: boolean }).cloud_models_off;
		}
		await route.fulfill({ json: { cloud_models_off: state.cloudOff } });
	});
	await page.route('**/api/settings/network-servers/*/private', (route) => state.failPrivate
		? route.fulfill({ status: 500, json: { detail: 'Server unavailable' } })
		: route.fulfill({ json: { ok: true } }));
	await page.route('**/api/settings/network-servers**', (route) => route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/settings/network-servers'
		? route.fulfill({ json: { servers: state.servers } }) : route.continue());
}

async function createProject(page: Page, name: string) {
	await page.keyboard.press('Control+k');
	await expect(page.locator('.palette')).toBeVisible();
	await page.locator('.palette .res').filter({ hasText: 'New project' }).click();
	const modal = page.getByRole('dialog');
	await modal.getByRole('textbox', { name: 'Name' }).fill(name);
	await modal.getByRole('button', { name: 'Create project' }).click();
	await expect(page.getByRole('button', { name: 'Project settings' })).toBeVisible({ timeout: 10_000 });
}

async function openProjectProviders(page: Page, name: string): Promise<Locator> {
	await createProject(page, name);
	await page.getByRole('button', { name: 'Project settings' }).click();
	const region = page.getByRole('region', { name: 'Project providers' });
	await expect(region).toBeVisible();
	await region.evaluate((element) => element.scrollIntoView({ block: 'start' }));
	return region;
}

async function openEndpoints(page: Page, serverName: string) {
	await openSettings(page);
	await openSettingsSection(page, 'Providers');
	const row = page.locator('.pr').filter({ hasText: serverName }).first();
	await expect(row).toBeVisible();
	await row.getByRole('button', { name: 'Settings', exact: true }).click();
	await page.getByRole('button', { name: 'Endpoints', exact: true }).click();
	await expect(page.getByText('Reachable at')).toBeVisible();
}

async function openSheetInProject(page: Page, name: string) {
	await createProject(page, name);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByRole('button', { name: 'New chat' }).first().click();
	await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
	await page.locator('button.model-chip').click();
	await expect(page.locator('.model-sheet')).toBeVisible();
}

test.describe('@surface:provider-policy local-only controls frames', () => {
	test.skip(!OUT, 'Capture-only; set LOCAL_ONLY_FRAMES_OUT to render.');
	test.setTimeout(240_000);

	test('renders every review frame', async ({ page }, testInfo) => {
		mkdirSync(OUT!, { recursive: true });
		const state: State = { models: catalogs.plain, servers: [servers.remoteOff], cloudOff: false, failSave: false };
		await serve(page, state);
		let stamp = 0;
		const shot = async (name: string, size: string, theme: string) => page.screenshot({ path: join(OUT!, `${name}-${size}-${theme}.png`), animations: 'disabled', scale: 'css' });
		const begin = async (width: number, height: number, theme: 'light' | 'dark') => {
			await page.setViewportSize({ width, height });
			await page.emulateMedia({ colorScheme: theme });
			await loginByUrl(page, { waitForComposer: false });
			stamp += 1;
			return `lo-${testInfo.project.name}-${stamp}`;
		};

		for (const [size, width, height, theme] of REFERENCE_ONLY ? [] : surfaces(testInfo.project.name)) {
			const prefix = BEFORE ? 'before-' : '';
			// Project page.
			Object.assign(state, { models: catalogs.plain, cloudOff: false, failSave: false });
			let region = await openProjectProviders(page, await begin(width, height, theme));
			if (!BEFORE) await expect(region.getByText('Cloud models off').first()).toBeVisible();
			await shot(`${prefix}project-page`, size, theme);
			if (!BEFORE) {
				Object.assign(state, { models: catalogs.someLocal, cloudOff: true });
				region = await openProjectProviders(page, await begin(width, height, theme));
				await expect(region.getByText('Agents here using a cloud model stop at their next turn')).toBeVisible();
				await shot('project-on-local-ready', size, theme);

				Object.assign(state, { models: catalogs.noneReady, cloudOff: true });
				region = await openProjectProviders(page, await begin(width, height, theme));
				await expect(region.getByText('No local model is ready, so nothing in this project can run until one is.')).toBeVisible();
				await shot('project-on-none-ready', size, theme);

				Object.assign(state, { models: catalogs.someLocal, cloudOff: false, failSave: true });
				region = await openProjectProviders(page, await begin(width, height, theme));
				await region.getByRole('button', { name: 'Cloud models off', exact: true }).click();
				await expect(region.getByText('Could not turn Cloud models off on: Server unavailable')).toBeVisible();
				await shot('project-save-failed', size, theme);
				state.failSave = false;

				Object.assign(state, { models: catalogs.someLocal, cloudOff: false });
				region = await openProjectProviders(page, await begin(width, height, theme));
				let release = () => {};
				state.hold = new Promise<void>((resolve) => { release = resolve; });
				const saving = region.getByRole('button', { name: 'Cloud models off', exact: true });
				await saving.click();
				await expect(saving).toBeDisabled();
				await shot('project-saving', size, theme);
				release(); state.hold = null;
			}

			// Endpoints tab.
			const endpoints: [string, unknown, string, (() => Promise<void>) | null][] = [
				['endpoints-remote-off', servers.remoteOff, 'oMLX', null],
				...(BEFORE ? [] : [
					['endpoints-remote-on', servers.remoteOn, 'oMLX', null],
					['endpoints-this-mac', servers.thisMac, 'oMLX', null],
					['endpoints-external-loopback', servers.loopback, 'OpenAI-compatible API', null],
				] as [string, unknown, string, null][]),
			];
			for (const [name, row, label] of endpoints) {
				state.servers = [row];
				await begin(width, height, theme);
				await openEndpoints(page, label);
				await shot(`${prefix}${name}`, size, theme);
			}

			if (!BEFORE) {
				for (const [name, row, label, act] of [
					['endpoints-private-save-failed', servers.remoteOff, 'oMLX', 'fail'],
					['endpoints-remote-offline', servers.remoteOffline, 'oMLX', 'none'],
				] as const) {
					Object.assign(state, { servers: [row], failPrivate: act === 'fail' });
					await begin(width, height, theme);
					await openEndpoints(page, label);
					if (act === 'fail') {
						await page.getByRole('button', { name: 'Private machine', exact: true }).click();
						await expect(page.getByText('Could not turn Private machine on: Server unavailable')).toBeVisible();
					}
					await shot(name, size, theme);
				}
				state.failPrivate = false;
			}

			// Composer model sheet in a cloud-off project.
			Object.assign(state, { models: catalogs.remoteNotPrivate, cloudOff: false });
			await openSheetInProject(page, await begin(width, height, theme));
			await shot(`${prefix}sheet-remote-not-private`, size, theme);
			if (!BEFORE) {
				for (const [name, models] of [['sheet-some-local', catalogs.someLocal], ['sheet-no-local', catalogs.noneReady], ['sheet-local-not-ready', catalogs.localDown]] as const) {
					Object.assign(state, { models, cloudOff: true });
					await openSheetInProject(page, await begin(width, height, theme));
					await shot(name, size, theme);
				}

				// A thread already on a cloud model when the project turns Cloud models off.
				Object.assign(state, { models: catalogs.plain, cloudOff: false });
				await openSheetInProject(page, await begin(width, height, theme));
				await page.locator('.model-sheet').getByRole('button', { name: /Opus 5\.5/ }).first().click();
				Object.assign(state, { models: catalogs.someLocal, cloudOff: true });
				await page.reload();
				await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
				await page.locator('button.model-chip').click();
				await expect(page.locator('.model-sheet')).toBeVisible();
				await shot('sheet-on-cloud-model', size, theme);
			}
		}

		// Reference: the chat canvas once, at 1440, light.
		if (!BEFORE && testInfo.project.name === 'chromium-desktop') {
			await page.unrouteAll({ behavior: 'ignoreErrors' });
			await page.setViewportSize({ width: 1440, height: 900 });
			await page.emulateMedia({ colorScheme: 'light' });
			await setFocusMode(page.request, false);
			await loginByUrl(page);
			await waitForWsConnected(page);
			await seedMockResponses(page.request, [{ content: 'Here is the outline: three sections, each with one open question for you to settle.', finish_reason: 'stop' }]);
			await sendChatMessage(page, 'Outline the client report for me.');
			await expect(assistantMessages(page).last()).toContainText('Here is the outline', { timeout: 30_000 });
			await shot('reference-chat-canvas', '1440', 'light');
		}
	});
});
