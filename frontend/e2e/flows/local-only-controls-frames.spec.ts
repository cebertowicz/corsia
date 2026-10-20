import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Locator, type Page } from '@playwright/test';
import { assistantMessages, loginByUrl, openSettings, openSettingsSection, sendChatMessage, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { setFocusMode } from '../support/focusMode';
import { seedMockResponses } from '../support/mock';

/**
 * Review frames for the local-only controls (Local models only, Trusted device, the cloud-off model sheet).
 * Every frame is the real app with the server answers faked by route interception; nothing signs in beyond the
 * disposable test token. Enabled only with LOCAL_ONLY_FRAMES_OUT (the output folder); LOCAL_ONLY_FRAMES_MODE=before
 * renders just the project page and Endpoints tab, for a checkout that does not have the change.
 */
const OUT = process.env.LOCAL_ONLY_FRAMES_OUT;
const BEFORE = process.env.LOCAL_ONLY_FRAMES_MODE === 'before';
const REFERENCE_ONLY = process.env.LOCAL_ONLY_FRAMES_MODE === 'reference';

const surfaces = (projectName: string) => projectName === 'chromium-desktop'
	? ([['1440', 1440, 900, 'light'], ['1440', 1440, 900, 'dark']] as const)
	: ([['393', 393, 844, 'light'], ['393', 393, 844, 'dark']] as const);

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
	privateExternalOnly: [cloudRow('opus', 'Opus 5.5', cloudOff), cloudRow('sonnet', 'Sonnet 5.5', cloudOff),
		modelRow({ id: 'external/gemma', selector: 'external@lms/gemma', litellm_model: 'external@lms/gemma', name: 'Gemma 3 27B', display_name: 'Gemma 3 27B',
			connection: 'external', provider: 'external', backend: 'network', server_name: 'LM Studio', status: 'ready', is_local: true })],
	localDown: [cloudRow('opus', 'Opus 5.5', cloudOff), cloudRow('sonnet', 'Sonnet 5.5', cloudOff),
		localRow('qwen', 'Qwen3 8B', 'This Mac', { status: 'unavailable', status_reason: 'missing_context_window' })],
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
		endpoints: [endpoint('e3', '127.0.0.1', 'local')], active_host: '127.0.0.1', on_this_mac: false, private: false, metadata: { base_url: 'http://127.0.0.1:1234/v1' } }),
	loopbackPrivate: server('srv-lms-private', { provider: 'external', kind: 'external', instance_name: 'LM Studio', port: 1234, dialect: 'openai',
		endpoints: [endpoint('e4', '127.0.0.1', 'local')], active_host: '127.0.0.1', on_this_mac: false, private: true, metadata: { base_url: 'http://127.0.0.1:1234/v1' } }),
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

type State = { models: unknown[]; servers: unknown[]; cloudOff: boolean; failSave: boolean; failPrivate?: boolean; canAddLocal?: boolean; hold?: Promise<void> | null };

async function serve(page: Page, state: State) {
	await page.routeWebSocket((url) => url.pathname === '/ws', (socket) => {
		const server = socket.connectToServer();
		socket.onMessage((payload) => server.send(payload));
		server.onMessage((payload) => {
			try {
				const frame = JSON.parse(String(payload));
				if (frame?.type === 'auth_ok' && frame.user && typeof frame.user === 'object') {
					socket.send(JSON.stringify({ ...frame, user: { ...frame.user, name: 'Sam' } }));
					return;
				}
				if (frame?.type === 'message_added' && frame.message?.role === 'assistant' && frame.message.metadata && typeof frame.message.metadata === 'object') {
					socket.send(JSON.stringify({ ...frame, message: { ...frame.message, metadata: { ...frame.message.metadata, model_name: 'Qwen3 8B' } } }));
					return;
				}
			} catch { /* Forward non-JSON frames unchanged. */ }
			socket.send(payload);
		});
	});
	await page.route('**/api/system/host-resources?**', (route) => route.fulfill({ json: {
		device_name: 'Studio', total_gb: 64, free_gb: 34, models_used_gb: 0, other_used_gb: 0, safe_free_gb: 8, safe_reserve_gb: 8, by_provider: {},
	} }));
	await page.route('**/api/models**', (route) => route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/models'
		? route.fulfill({ json: { models: state.models, active_local_threads: 0 } }) : route.continue());
	await page.route('**/api/threads/*/messages?**', async (route) => {
		if (route.request().method() !== 'GET') return route.continue();
		const response = await route.fetch();
		const body = await response.json() as { messages: Array<Record<string, unknown>> };
		await route.fulfill({ response, json: { ...body, messages: body.messages.map((message) => message.role === 'assistant'
			? { ...message, metadata: { ...(message.metadata as Record<string, unknown> | undefined), model_name: 'Qwen3 8B' } }
			: message) } });
	});
	// Only the frames that show "Add a local model" fake this computer's setup answer; the rest read the disposable backend's own.
	await page.route('**/api/providers/sources**', (route) => state.canAddLocal && route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/providers/sources'
		? route.fulfill({ json: { sources: [{ source: 'omlx', name: 'oMLX', state: 'needs_setup', eligible: true }] } }) : route.continue());
	await page.route('**/api/vendor-accounts**', (route) => route.request().method() === 'GET' ? route.fulfill({ json: { accounts: vendorAccounts } }) : route.continue());
	await page.route('**/api/provider-policy**', (route) => route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/provider-policy'
		? route.fulfill({ json: policy }) : route.continue());
	await page.route('**/api/projects/*/cloud-models-off**', async (route) => {
		if (route.request().method() === 'PUT') {
			if (state.hold) await state.hold;
			if (state.failSave) return route.abort();
			state.cloudOff = (route.request().postDataJSON() as { cloud_models_off: boolean }).cloud_models_off;
		}
		await route.fulfill({ json: { cloud_models_off: state.cloudOff } });
	});
	await page.route('**/api/projects?**', async (route) => {
		if (route.request().method() !== 'POST') return route.continue();
		const response = await route.fetch();
		const body = await response.json() as { project: Record<string, unknown> };
		await route.fulfill({ response, json: { project: { ...body.project, accent_color: '#007faa' } } });
	});
	await page.route('**/api/settings/network-servers**', (route) => route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/settings/network-servers'
		? route.fulfill({ json: { servers: state.servers } }) : route.continue());
	await page.route('**/api/settings/network-servers/*/private**', (route) => state.failPrivate
		? route.abort()
		: route.fulfill({ json: { ok: true } }));
}

async function createProject(page: Page, name: string): Promise<{ id: string }> {
	await page.keyboard.press('Control+k');
	await expect(page.locator('.palette')).toBeVisible();
	await page.locator('.palette .res').filter({ hasText: 'New project' }).click();
	const modal = page.getByRole('dialog');
	await modal.getByRole('textbox', { name: 'Name' }).fill(name);
	const created = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/projects' && response.request().method() === 'POST');
	await modal.getByRole('button', { name: 'Create project' }).click();
	const project = (await (await created).json()).project as { id: string };
	await expect(modal).toBeHidden();
	// Select the new project explicitly: a frame sequence can already have another project in scope.
	await page.keyboard.press('Control+k');
	await expect(page.locator('.palette')).toBeVisible();
	await page.locator('.palette').getByRole('button', { name, exact: true }).click();
	await expect(page.getByRole('button', { name: 'Project settings' })).toBeVisible({ timeout: 10_000 });
	return project;
}

async function openProjectProviders(page: Page, name: string): Promise<Locator> {
	await createProject(page, name);
	await page.getByRole('button', { name: 'Project settings' }).click();
	const region = page.getByRole('region', { name: 'Project providers' });
	await expect(region).toBeVisible();
	await region.evaluate((element) => element.scrollIntoView({ block: 'start' }));
	return region;
}

/** Seed the long-name layout case through the real disposable backend, not the modal-response fixture. */
async function seedProject(page: Page, name: string): Promise<{ id: string }> {
	const params = new URLSearchParams({ token: TEST_TOKEN });
	const response = await page.request.post(`/api/projects?${params}`, {
		data: { name, description: '', accent_color: '#007faa' },
	});
	expect(response.ok()).toBeTruthy();
	return (await response.json()).project as { id: string };
}

async function openSeededProjectProviders(page: Page, name: string): Promise<Locator> {
	await page.keyboard.press('Control+k');
	await expect(page.locator('.palette')).toBeVisible();
	await page.locator('.palette').getByRole('button', { name, exact: true }).click();
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
	await expect(page.locator('.empty-chat h2')).toHaveText('Hey, Sam.');
	await page.locator('button.model-chip').click();
	await expect(page.locator('.model-sheet')).toBeVisible();
}

async function openHeldCloudOffChat(page: Page, state: State, projectName: string) {
	const chatName = 'Interview summary';
	const projectResponse = await page.request.post(`/api/projects?token=${encodeURIComponent(TEST_TOKEN)}`, {
		data: { name: projectName, description: '', accent_color: '#007faa' },
	});
	expect(projectResponse.ok()).toBeTruthy();
	const project = (await projectResponse.json()).project as { id: string };
	const created = await page.request.post(`/api/threads?token=${encodeURIComponent(TEST_TOKEN)}`, {
		data: { title: chatName, project_id: project.id, model: 'claude-code/opus' },
	});
	expect(created.ok()).toBeTruthy();
	const threadId = (await created.json()).thread_id as string;
	await page.route(`**/api/threads/${threadId}/messages?**`, (route) => route.fulfill({ json: {
		messages: [{
			id: `${threadId}:held`, thread_id: threadId, role: 'user', content: 'Please summarize the customer interviews.',
			created_at: '2026-10-19T00:00:00Z', delivery_state: 'queued', delivery_error: 'project_cloud_off',
			write_outcome: 'proved_no_write', active_turn_id: null, metadata: { delivery_intent: 'apply_now' },
		}], has_more: false, oldest_id: null,
	} }));
	Object.assign(state, { models: catalogs.someLocal, cloudOff: true });
	await loginByUrl(page, { waitForComposer: false });
	await page.keyboard.press('Control+k');
	await expect(page.locator('.palette')).toBeVisible();
	await page.locator('.palette').getByRole('button', { name: projectName, exact: true }).click();
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-title', { hasText: chatName }).click();
	await expect(page.locator('.delivery-status')).toHaveText('Switch to a local model to send');
}

test.describe('@surface:provider-policy local-only controls frames', () => {
	test.skip(!OUT, 'Capture-only; set LOCAL_ONLY_FRAMES_OUT to render.');
	test.setTimeout(240_000);

	test('renders every review frame', async ({ page }, testInfo) => {
		mkdirSync(OUT!, { recursive: true });
		const state: State = { models: catalogs.plain, servers: [servers.remoteOff], cloudOff: false, failSave: false };
		await serve(page, state);
		const longProjectName = 'Western Washington workflow workshop working group workspace';
		// Both browser projects share this disposable backend; seed once so the real-create check remains singular.
		if (testInfo.project.name === 'chromium-desktop') await seedProject(page, longProjectName);
		let stamp = 0;
		const projectThemes = [
			'Market research', 'Product planning', 'Design review', 'Launch preparation',
			'Support themes', 'Sales notes', 'Interview notes', 'Competitive analysis',
		] as const;
		const projectMonths = testInfo.project.name === 'chromium-desktop'
			? ['January', 'February', 'March', 'April', 'May', 'June'] as const
			: ['July', 'August', 'September', 'October', 'November', 'December'] as const;
		const shot = async (name: string, size: string, theme: string) => {
			// A frame counts only with the exact committed head in its dev header: wait for the real hash, no placeholder, no dirty mark.
			await expect(page.locator('.version-overlay')).toHaveText(/^\s*b [0-9a-f]{7}\s*·\s*f [0-9a-f]{7}\s*·/, { timeout: 15_000 });
			await page.screenshot({ path: join(OUT!, `${name}-${size}-${theme}.png`), animations: 'disabled', scale: 'css' });
		};
		const begin = async (width: number, height: number, theme: 'light' | 'dark') => {
			await page.setViewportSize({ width, height });
			await page.emulateMedia({ colorScheme: theme });
			await loginByUrl(page, { waitForComposer: false });
			stamp += 1;
			const index = stamp - 1;
			return `${projectThemes[index % projectThemes.length]} ${projectMonths[Math.floor(index / projectThemes.length)]}`;
		};

		for (const [size, width, height, theme] of REFERENCE_ONLY ? [] : surfaces(testInfo.project.name)) {
			const prefix = BEFORE ? 'before-' : '';
			// Project page.
			Object.assign(state, { models: catalogs.plain, cloudOff: false, failSave: false });
			let region = await openProjectProviders(page, await begin(width, height, theme));
			if (!BEFORE) await expect(region.getByText('Local models only').first()).toBeVisible();
			await shot(`${prefix}project-page`, size, theme);
			if (!BEFORE) {
				Object.assign(state, { models: catalogs.plain, cloudOff: false, failSave: false });
				await begin(width, height, theme);
				region = await openSeededProjectProviders(page, longProjectName);
				const scopeName = page.locator('.shell-header .h-scope');
				await expect(scopeName).toHaveText(longProjectName);
				await expect(scopeName).toHaveCSS('text-overflow', 'ellipsis');
				expect(await scopeName.evaluate((element) => element.scrollWidth > element.clientWidth)).toBeTruthy();
				await shot('project-long-name', size, theme);
			}
			if (!BEFORE) {
				Object.assign(state, { models: catalogs.someLocal, cloudOff: true });
				region = await openProjectProviders(page, await begin(width, height, theme));
				await expect(region.getByText('Chats and agents in this project use only models on this computer or on a trusted device. A chat on a cloud model waits until you switch it to a local one.')).toBeVisible();
				await shot('project-on-local-ready', size, theme);

				Object.assign(state, { models: catalogs.noneReady, cloudOff: true });
				region = await openProjectProviders(page, await begin(width, height, theme));
				await expect(region.getByText('No local model is ready, so nothing in this project can run.')).toBeVisible();
				await shot('project-on-none-ready', size, theme);

				Object.assign(state, { models: catalogs.noneReady, cloudOff: false });
				region = await openProjectProviders(page, await begin(width, height, theme));
				await expect(region.getByText('No local model is ready yet.')).toBeVisible();
				await shot('project-off-none-ready', size, theme);

				Object.assign(state, { models: catalogs.someLocal, cloudOff: false, failSave: true });
				region = await openProjectProviders(page, await begin(width, height, theme));
				await region.getByRole('button', { name: 'Local models only', exact: true }).click();
				await expect(region.getByText('Couldn’t save this change. Check your connection and try again.')).toBeVisible();
				await shot('project-save-failed', size, theme);

				Object.assign(state, { models: catalogs.someLocal, cloudOff: true, failSave: true });
				region = await openProjectProviders(page, await begin(width, height, theme));
				await region.getByRole('button', { name: 'Local models only', exact: true }).click();
				await expect(region.getByText('Couldn’t save this change. Check your connection and try again.')).toBeVisible();
				await shot('project-save-failed-off', size, theme);
				state.failSave = false;

				Object.assign(state, { models: catalogs.someLocal, cloudOff: false });
				region = await openProjectProviders(page, await begin(width, height, theme));
				let release = () => {};
				state.hold = new Promise<void>((resolve) => { release = resolve; });
				const saving = region.getByRole('button', { name: 'Local models only', exact: true });
				await saving.click();
				await expect(saving).toBeDisabled();
				await shot('project-saving', size, theme);
				release(); state.hold = null;
			}

			if (!BEFORE) {
				await begin(width, height, theme);
				const heldProjectName = `Customer research ${projectMonths[Math.floor((stamp - 1) / projectThemes.length)]}`;
				await openHeldCloudOffChat(page, state, heldProjectName);
				await shot('chat-held-cloud-off', size, theme);
			}

			// Endpoints tab.
			const endpoints: [string, unknown, string, (() => Promise<void>) | null][] = [
				['endpoints-remote-off', servers.remoteOff, 'oMLX', null],
				...(BEFORE ? [] : [
					['endpoints-remote-on', servers.remoteOn, 'oMLX', null],
					['endpoints-this-mac', servers.thisMac, 'oMLX', null],
					['endpoints-external-loopback', servers.loopback, 'OpenAI-compatible API', null],
					['endpoints-external-loopback-private', servers.loopbackPrivate, 'OpenAI-compatible API', null],
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
						await page.getByRole('button', { name: 'Trusted device', exact: true }).click();
						await expect(page.getByText('Couldn’t save this change. Check your connection and try again.')).toBeVisible();
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
				for (const [name, models, canAddLocal] of [
					['sheet-some-local', catalogs.someLocal, false], ['sheet-no-local', catalogs.noneReady, true],
					['sheet-local-not-ready', catalogs.localDown, false], ['sheet-private-external-only', catalogs.privateExternalOnly, true],
				] as const) {
					Object.assign(state, { models, cloudOff: true, canAddLocal });
					await openSheetInProject(page, await begin(width, height, theme));
					if (canAddLocal) await expect(page.locator('.model-sheet').getByRole('button', { name: 'Add a local model' })).toBeVisible();
					await shot(name, size, theme);
				}

				state.canAddLocal = false;

				// A thread already on a cloud model when the project turns Local models only.
				Object.assign(state, { models: catalogs.plain, cloudOff: false });
				await openSheetInProject(page, await begin(width, height, theme));
				await page.locator('.model-sheet').getByRole('button', { name: /Opus 5\.5/ }).first().click();
				await expect(page.locator('button.model-chip')).toContainText('Opus 5.5');
				Object.assign(state, { models: catalogs.someLocal, cloudOff: true });
				await page.evaluate(async () => (await import('/src/lib/stores/models.ts')).loadModels());
				await expect(page.locator('button.model-chip')).toBeVisible();
				if (!(await page.locator('.model-sheet').isVisible())) await page.locator('button.model-chip').click();
				await expect(page.locator('.model-sheet')).toBeVisible();
				await shot('sheet-on-cloud-model', size, theme);
			}
		}

		// Reference: the chat canvas once, at 1440, light.
		if (!BEFORE && testInfo.project.name === 'chromium-desktop') {
			// Keep the backend's executable mock model, but give its visible catalog row the real name used in the sheet frames.
			await page.unroute('**/api/models**');
			await page.route('**/api/models**', async (route) => {
				if (route.request().method() !== 'GET' || new URL(route.request().url()).pathname !== '/api/models') return route.continue();
				const response = await route.fetch();
				const body = await response.json() as { models: Record<string, unknown>[] };
				await route.fulfill({ response, json: { ...body, models: body.models.map((model) => ({ ...model, name: 'Qwen3 8B', display_name: 'Qwen3 8B' })) } });
			});
			await page.setViewportSize({ width: 1440, height: 900 });
			await page.emulateMedia({ colorScheme: 'light' });
			await setFocusMode(page.request, false);
			await loginByUrl(page);
			await waitForWsConnected(page);
			await expect(page.locator('button.model-chip')).toContainText('Qwen3 8B');
			await seedMockResponses(page.request, [{ content: 'Here is the outline: three sections, each with one open question for you to settle.', finish_reason: 'stop' }]);
			await sendChatMessage(page, 'Outline the client report for me.');
			await expect(assistantMessages(page).last()).toContainText('Here is the outline', { timeout: 30_000 });
			await expect(page.locator('.msg-model-badge').last()).toHaveText('Qwen3 8B');
			await expect(page.getByText('Mock Pressure Model', { exact: true })).toHaveCount(0);
			await shot('reference-chat-canvas', '1440', 'light');
		}
	});
});
