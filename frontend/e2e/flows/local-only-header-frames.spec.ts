import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { loginByUrl } from '../support/corsia';

/** Review frames use the app's actual header and model sheet with only catalog and thread policy answers faked. */
const OUT = process.env.LOCAL_ONLY_HEADER_FRAMES_OUT;
const BEFORE = process.env.LOCAL_ONLY_HEADER_FRAMES_MODE === 'before';

const surfaces = (projectName: string) => projectName === 'chromium-desktop'
	? ([['1440', 1440, 900, 'light'], ['1440', 1440, 900, 'dark']] as const)
	: ([['393', 393, 844, 'light'], ['393', 393, 844, 'dark']] as const);

const capsule = { context_window_max: 200000, supports_vision: true, supports_tools: true, supports_reasoning: true, reasoning_config: null };
const row = (over: Record<string, unknown>) => ({ desc: '', size: '', speed: '', capabilities: capsule, available: true, ...over });
const cloud = (name: string) => row({ id: `claude-code/${name}`, selector: `claude-code/${name}`, litellm_model: `claude-code/${name}`, name: `Claude Code · ${name === 'opus' ? 'Opus 5.5' : 'Sonnet 5.5'}`, display_name: name === 'opus' ? 'Opus 5.5' : 'Sonnet 5.5', connection: 'claude-code', provider: 'claude-code', backend: 'cc', status: 'ready', is_local: false });
const local = (name: string, serverName: string, over: Record<string, unknown> = {}) => row({ id: `omlx/${name}`, selector: `omlx@${name}`, litellm_model: `omlx@${name}`, name: name === 'qwen' ? 'Qwen3 8B' : 'Llama 3.3 70B', display_name: name === 'qwen' ? 'Qwen3 8B' : 'Llama 3.3 70B', connection: 'omlx', provider: 'omlx', backend: 'network', server_name: serverName, status: 'ready', is_local: true, ...over });

const catalogs = {
	ordinary: [cloud('opus'), cloud('sonnet'), local('qwen', 'This Mac'), local('llama', 'Studio mini')],
	project: [
		{ ...cloud('opus'), status: 'unavailable', status_reason: 'project_cloud_off' },
		{ ...cloud('sonnet'), status: 'unavailable', status_reason: 'project_cloud_off' },
		local('qwen', 'This Mac'), local('llama', 'Studio mini'),
	],
	thread: [
		{ ...cloud('opus'), status: 'unavailable', status_reason: 'thread_local_only' },
		{ ...cloud('sonnet'), status: 'unavailable', status_reason: 'thread_local_only' },
		local('qwen', 'This Mac'), local('llama', 'Studio mini'),
	],
	none: [cloud('opus'), cloud('sonnet'), local('qwen', 'This Mac', { status: 'unavailable', status_reason: 'missing_context_window' })],
};

type FrameState = { localOnly?: boolean; inherited?: boolean; project?: boolean; noLocal?: boolean; cloudModel?: boolean; failSave?: boolean; pendingSave?: boolean; chatTitle?: string; parentTitle?: string };

const longChatTitle = 'Regional research synthesis, source review, policy analysis, decision preparation, and stakeholder presentation notes';
const longParentTitle = 'Regional research synthesis, source review, policy analysis, and decision preparation';

async function serve(page: Page, state: { current: FrameState }) {
	let releasePendingSave: (() => void) | null = null;
	// The empty-chat greeting takes its name from the auth_ok socket payload. Keep the
	// disposable test account intact and make the rendered review identity realistic.
	await page.routeWebSocket('**/ws', (socket) => {
		const server = socket.connectToServer();
		socket.onMessage((message) => {
			if (typeof message === 'string') {
				const payload = JSON.parse(message);
				// The synthetic review thread is not persisted by the disposable backend.
				// Its HTTP reads are already mocked below, so its subscription must not
				// prompt a server-side \"thread not found\" error in the empty transcript.
				if (payload.type === 'subscribe_session' && payload.thread_id === 'local-only-review') return;
			}
			server.send(message);
		});
		server.onMessage((message) => {
			if (typeof message !== 'string') return socket.send(message);
			const payload = JSON.parse(message);
			// A reconnect can subscribe after the synthetic thread store is installed.
			// The disposable backend has no such thread, so exclude only that fixture
			// error rather than letting it replace the intentionally empty transcript.
			if (payload.type === 'error' && payload.thread_id === 'local-only-review') return;
			if (payload.type === 'auth_ok' && payload.user) payload.user.name = 'Sam';
			socket.send(JSON.stringify(payload));
		});
	});
	await page.route('**/api/models**', (route) => {
		const url = new URL(route.request().url());
		if (route.request().method() !== 'GET' || url.pathname !== '/api/models') return route.continue();
		const scopedLocalOnly = url.searchParams.get('thread_id') === 'local-only-review'
			&& (state.current.localOnly === true || state.current.inherited === true);
		const models = state.current.project ? catalogs.project : state.current.noLocal ? catalogs.none : scopedLocalOnly ? catalogs.thread : catalogs.ordinary;
		return route.fulfill({ json: { models, active_local_threads: 0 } });
	});
	await page.route('**/api/plans/operator/threads/local-only-review', (route) => route.fulfill({ json: { contract_version: 2, as_of: '2026-10-19T00:00:00Z', target: { kind: 'thread', id: 'local-only-review', label: 'Research review' }, revision_token: 'local-only-review', plans: [], attention: { awaiting_count: 0 } } }));
	// The artifact pane can finish a prior thread's initial read while the frame fixture
	// swaps in its thread. Give every pane read the same valid empty payload before the
	// app mounts, so no frame captures an in-flight sidebar.
	await page.addInitScript(() => {
		const nativeFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(input instanceof Request ? input.url : String(input), window.location.origin);
			if (/^\/api\/threads\/[^/]+\/artifacts$/.test(url.pathname)) {
				return Promise.resolve(new Response(JSON.stringify({ nodes: [], needs_attention: [] }), {
					status: 200,
					headers: { 'Content-Type': 'application/json' },
				}));
			}
			return nativeFetch(input, init);
		};
	});
	// Login starts a first-page read for the disposable account's initial thread before
	// the fixture replaces it. Keep every transcript read valid and empty so that stale
	// load cannot leave the synthetic review transcript in its loading state.
	await page.route('**/api/threads/*/messages**', (route) => route.fulfill({ json: { messages: [], has_more: false, oldest_id: null, turn_summaries: {} } }));
	await page.route('**/api/threads/**/local-only**', async (route) => {
		if (state.current.failSave) return route.abort();
		if (state.current.pendingSave) await new Promise<void>((resolve) => { releasePendingSave = resolve; });
		return route.fulfill({ json: { ok: true, local_only: Boolean(state.current.localOnly || state.current.pendingSave), local_only_inherited_from_thread_id: null } });
	});
	return { releasePendingSave: () => releasePendingSave?.() };
}

async function openChat(page: Page, state: FrameState) {
	await loginByUrl(page);
	await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
	await page.evaluate(async (next) => {
		const { activeThreadId, messages, threads } = await import('/src/lib/stores/threads.ts');
		const { uiScope } = await import('/src/lib/stores/uiScope.ts');
		const { loadModels, setActiveModel } = await import('/src/lib/stores/models.ts');
		const { requestModelSheet } = await import('/src/lib/stores/modelSheetRequest.ts');
		const id = 'local-only-review';
		// Login can receive a background WebSocket error before this synthetic
		// empty-chat state is installed; it must not bleed into a review frame.
		messages.set([]);
		activeThreadId.set(id);
		uiScope.set(next.project ? { id: 'project-local', name: 'Local analysis', color: '#007faa' } : null);
		threads.set([{
			id, user_id: 'e2e', profile: 'personal', title: next.inherited ? 'Delegated analysis' : next.chatTitle ?? 'Research review',
			model: next.cloudModel ? 'claude-code/opus' : 'omlx@qwen', project_id: next.project ? 'project-local' : null,
			created_at: '2026-10-19T00:00:00Z', updated_at: '2026-10-19T00:00:00Z',
			// N1 workers retain their own stored Local only value and name the inherited source too.
			local_only: next.localOnly === true || next.inherited === true,
			local_only_inherited_from_thread_id: next.inherited ? 'parent-chat' : null,
			local_only_inherited_from_title: next.inherited ? next.parentTitle ?? 'Research review' : null,
		}]);
		setActiveModel(next.cloudModel ? 'claude-code/opus' : 'omlx@qwen');
		await loadModels();
		requestModelSheet();
	}, state);
	const sheet = page.locator('.model-sheet');
	await expect(sheet).toBeVisible();
	await expect(page.getByText('Loading this thread…')).toHaveCount(0);
	await expect(page.locator('.empty-chat')).toBeVisible();
	const emptyArtifactPane = page.getByText('Files you create, upload, or open in this thread show up here.');
	await expect(emptyArtifactPane).toHaveCount(1);
	if ((page.viewportSize()?.width ?? 0) > 768) await expect(emptyArtifactPane).toBeVisible();
	if (BEFORE) await page.addStyleTag({ content: '.local-only-setting { display: none !important; }' });
}

async function screenshot(page: Page, name: string, size: string, theme: string) {
	await expect(page.locator('.version-overlay')).toHaveText(/^\s*b [0-9a-f]{7}\s*·\s*f [0-9a-f]{7}\s*·/, { timeout: 15_000 });
	await expect(page.getByRole('heading', { name: 'Hey, Sam.' })).toBeVisible();
	expect(await page.locator('body').innerText()).not.toMatch(/\be2e\b|test[- ]run/i);
	await page.screenshot({ path: join(OUT!, `${name}-${size}-${theme}.png`), animations: 'disabled', scale: 'css' });
}

test.describe('@surface:local-only local-only header frames', () => {
	test.skip(!OUT, 'Capture-only; set LOCAL_ONLY_HEADER_FRAMES_OUT to render.');
	test.setTimeout(240_000);

	test('renders every review frame', async ({ page }, testInfo) => {
		mkdirSync(OUT!, { recursive: true });
		const state = { current: {} as FrameState };
		const server = await serve(page, state);
		for (const [size, width, height, theme] of surfaces(testInfo.project.name)) {
			await page.setViewportSize({ width, height });
			await page.emulateMedia({ colorScheme: theme });
			const frames: [string, FrameState, string][] = BEFORE
				? [['ordinary', {}, 'This chat and its workers use local models only.']]
				: [
					['ordinary', {}, 'This chat and its workers use local models only.'],
					['cloud-model', { cloudModel: true }, 'Switch this chat to a local model first.'],
					['local-only-on', { localOnly: true }, 'This chat and its workers use local models only.'],
					['inherited-worker', { localOnly: true, inherited: true }, 'Set by Research review'],
					['project-local-only', { project: true }, 'Set by this project'],
					['no-local-ready', { noLocal: true }, 'No local model is ready.'],
					['write-in-flight', { pendingSave: true }, 'This chat and its workers use local models only.'],
					['save-failure', { failSave: true }, "This chat and its workers use local models only."],
				];
			for (const [name, next, text] of frames) {
				state.current = next;
				await openChat(page, next);
				const sheet = page.locator('.model-sheet');
				await expect(sheet).toContainText(text);
				if (['local-only-on', 'inherited-worker', 'project-local-only'].includes(name)) {
					await expect(sheet.getByText('Opus 5.5', { exact: true })).toHaveCount(0);
					await expect(sheet.getByText('Sonnet 5.5', { exact: true })).toHaveCount(0);
				}
				if (['inherited-worker', 'project-local-only'].includes(name)) {
					await expect(sheet.locator('.tg')).toHaveClass(/held/);
					await expect(sheet.locator('.tg')).not.toHaveClass(/locked/);
				}
				if (name === 'write-in-flight') {
					await page.getByRole('button', { name: 'Local only', exact: true }).click();
					await expect(page.getByRole('button', { name: 'Local only', exact: true })).toHaveAttribute('aria-pressed', 'true');
					await expect(page.getByText('Saving…', { exact: true })).toBeVisible();
					await expect(sheet.locator('.tg')).toHaveClass(/locked/);
					await expect(sheet.locator('.tg')).not.toHaveClass(/held/);
					await expect(page.locator('.h-local-only')).toHaveCount(0);
				} else if (name === 'save-failure') {
					await page.getByRole('button', { name: 'Local only', exact: true }).click();
					await expect(page.getByText("Couldn't save this change. Check your connection and try again.")).toBeVisible();
					await expect(page.locator('.h-local-only')).toHaveCount(0);
				}
				await screenshot(page, `${BEFORE ? 'before-' : ''}${name}`, size, theme);
				if (name === 'write-in-flight') server.releasePendingSave();
			}
			if (!BEFORE) {
				const longFrames: [string, FrameState, string][] = testInfo.project.name === 'chromium-desktop'
					? [['local-only-on-long-title', { localOnly: true, chatTitle: longChatTitle }, 'This chat and its workers use local models only.']]
					: [
						['local-only-on-long-title', { localOnly: true, chatTitle: longChatTitle }, 'This chat and its workers use local models only.'],
						['inherited-worker-long-parent', { localOnly: true, inherited: true, parentTitle: longParentTitle }, `Set by ${longParentTitle}`],
					];
				for (const [name, next, text] of longFrames) {
					state.current = next;
					await openChat(page, next);
					await expect(page.locator('.model-sheet')).toContainText(text);
					await expect(page.locator('.h-local-only')).toBeVisible();
					if (name === 'local-only-on-long-title') {
						expect(await page.locator('.h-scope').evaluate((element) => element.scrollWidth > element.clientWidth)).toBeTruthy();
					} else {
						expect(await page.locator('#local-only-description').evaluate((element) => element.scrollWidth <= element.clientWidth)).toBeTruthy();
					}
					await screenshot(page, name, size, theme);
				}
			}
		}
		if (!BEFORE) {
			for (const [size, width, height, theme] of surfaces(testInfo.project.name)) {
				state.current = { localOnly: true };
				await page.setViewportSize({ width, height });
				await page.emulateMedia({ colorScheme: theme });
				await openChat(page, state.current);
				await page.getByRole('button', { name: 'Local only. Change in model settings.' }).click();
				await expect(page.locator('.local-only-setting')).toBeFocused();
				await screenshot(page, 'badge-tapped', size, theme);
			}
		}
		if (!BEFORE && testInfo.project.name === 'chromium-desktop') {
			state.current = {};
			await page.setViewportSize({ width: 1440, height: 900 });
			await page.emulateMedia({ colorScheme: 'light' });
			await loginByUrl(page);
			await page.getByRole('button', { name: 'New chat' }).first().click();
			await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
			await screenshot(page, 'reference-chat-canvas', '1440', 'light');
		}
	});
});
