import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { resolve } from 'node:path';
import { chatComposer, sendButton, TEST_SESSION_TOKEN, waitForWsConnected } from '../support/corsia';
import { seedMockResponses } from '../support/mock';
import { setFocusMode } from '../support/focusMode';

// Same disposable app landing and setup route as setup-landing.spec.ts: the
// model pick, not an injected history message, creates the first-run opening.
async function seedAppLanding() {
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (!python || !workspace || !state) throw new Error('Requires disposable e2e-local roots');
	await promisify(execFile)(python, ['-c', [
		'from infra import config, user_profile',
		'from infra.corsia_agent import get_corsia_agent',
		'from infra.database import write_transaction',
		'from infra.onboarding_events import enqueue_first_run_wake',
		'from onboarding.app_first_run import seed_initial',
		'from plans import public',
		'profile = user_profile.stage_profile_update(config.WORKSPACE, full_name="First Person")',
		'if profile is not None: profile.publish()',
		'with write_transaction(None, scope="e2e_app_landing") as db:',
		'    thread_id = get_corsia_agent(db=db)',
		'    owner = db.execute("SELECT user_id,model FROM threads WHERE id=?", (thread_id,)).fetchone()',
		'    assert owner["model"] is None',
		'    assert db.execute("SELECT 1 FROM messages WHERE thread_id=? AND seq IS NOT NULL", (thread_id,)).fetchone() is None',
		'    actor = public.PlanActor(thread_id, owner["user_id"], None)',
		'    listing = public.ensure_current_plan(actor=actor, include_folded=True, db=db)',
		'    for node in listing["nodes"]:',
		'        if node.get("archived_at") is not None: continue',
		'        current = public.get_node(node["id"], db=db)',
		'        public.mutate(listing["plan"]["id"], "close_node", {"node_id": node["id"], "expected_revision": current.revision, "reason": "Disposable app setup fixture"}, actor=actor, provenance="test.app_landing", mutation_id="app-close:" + node["id"], db=db)',
		'        current = public.get_node(node["id"], db=db)',
		'        public.mutate(listing["plan"]["id"], "archive_node", {"node_id": node["id"], "expected_revision": current.revision}, actor=actor, provenance="test.app_landing", mutation_id="app-archive:" + node["id"], db=db)',
		'    db.execute("DELETE FROM messages WHERE thread_id=? AND seq IS NULL", (thread_id,))',
		'    seed_initial(thread_id, owner["user_id"], db=db)',
		'    assert enqueue_first_run_wake(thread_id, first_run_path="app", db=db)',
	].join('\n')], { cwd: resolve('..', 'backend'), env: { ...process.env, CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state, CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1' }, timeout: 15_000 });
}

async function serveReadySetup(page: Page) {
	const sources = [{ source: 'claude-code', name: 'Claude Code', state: 'ready' }];
	await page.route('**/api/auth/status', route => route.fulfill({ json: { has_users: false } }));
	await page.route('**/api/setup/status', route => route.fulfill({ json: { apple_silicon: true, macos_supported: true, local_eligible: true, work_folder: '~/CORSIA' } }));
	await page.route('**/api/setup/progress', route => route.fulfill({ json: { setup_completed: false, step: 'models', local_ai: false } }));
	await page.route(url => url.pathname === '/api/models', route => route.request().method() === 'GET' ? route.fulfill({ json: { models: [{ id: 'mock/test-model', litellm_model: 'mock/test-model', name: 'Vendor default', connection: 'claude-code', provider: 'claude-code', backend: 'api', status: 'ready', available: true, is_default: true, runs_on: [{ account_id: 'disposable-account', selected: true, status: 'ready' }] }], active_local_threads: 0 } }) : route.continue());
	await page.route('**/api/setup/local-model', route => route.fulfill({ json: { state: 'picked', model: { name: 'Qwen 3.8', repository: 'example/qwen', download_size_bytes: 30_000_000_000, download_size_gb: 30, context_window: 65536 }, message: null } }));
	await page.route('**/api/setup/local-download', route => route.fulfill({ json: { ok: true, in_progress: true } }));
	await page.route('**/api/setup/first-account', route => route.fulfill({ json: { token: TEST_SESSION_TOKEN, user: { id: 'e2e', name: 'First Person' } } }));
	await page.route('**/api/providers/sources/recheck**', route => route.fulfill({ json: { sources } }));
	await page.route('**/api/providers/sources**', route => route.fulfill({ json: { sources } }));
}

async function inTranscript(page: Page, selector: string) {
	const message = page.locator(selector);
	const [box, transcript, composer] = await Promise.all([message.boundingBox(), page.locator('.messages').boundingBox(), page.locator('.input-floater').boundingBox()]);
	expect(box, `${selector} exists`).not.toBeNull();
	expect(transcript, 'transcript exists').not.toBeNull();
	expect(composer, 'composer exists').not.toBeNull();
	const bottom = Math.min(transcript!.y + transcript!.height, composer!.y, page.viewportSize()!.height);
	expect(box!.y, `${selector} top inside transcript`).toBeGreaterThanOrEqual(Math.max(0, transcript!.y));
	expect(box!.y + box!.height, `${selector} bottom above composer`).toBeLessThanOrEqual(bottom);
}

test.describe('first send follows the live first-run opening', () => {
	test.use({ seedPerTest: true });
	test.afterEach(async ({ request }) => { await setFocusMode(request, true); });
	test('sent message and reply come into view', async ({ page, request }, info) => {
		test.setTimeout(120_000);
		const size = info.project.name === 'chromium-mobile' ? { width: 393, height: 852 } : { width: 1440, height: 900 };
		await page.setViewportSize(size);
		await seedAppLanding();
		await setFocusMode(request, false);
		await serveReadySetup(page);
		const socket = page.waitForEvent('websocket', { predicate: ws => new URL(ws.url()).pathname === '/ws', timeout: 20_000 });
		await page.goto('/');
		await expect(page.getByRole('heading', { name: 'Welcome to CORSIA' })).toBeVisible();
		await page.getByRole('button', { name: 'Continue' }).click();
		await page.locator('input[autocomplete="name"]').fill('First Person');
		await page.locator('input[autocomplete="username"]').fill('first');
		await page.locator('input[autocomplete="new-password"]').nth(0).fill('correct-password');
		await page.locator('input[autocomplete="new-password"]').nth(1).fill('correct-password');
		await page.getByRole('button', { name: 'Continue' }).click();
		await page.getByRole('button', { name: /^(No|Neither)$/ }).click();
		await page.getByRole('button', { name: 'Start using CORSIA' }).click();
		await expect(chatComposer(page)).toBeEnabled({ timeout: 20_000 });
		await socket;
		await seedMockResponses(request, [{ content: 'FIRST_RUN_MOCK_REPLY', finish_reason: 'stop' }, { content: 'FIRST_SEND_MOCK_REPLY', finish_reason: 'stop' }]);
		const chip = page.locator('button.model-chip');
		await expect(chip).toContainText('Select Model');
		await chip.click();
		await page.locator('button.model-list-item', { hasText: 'Vendor default' }).click();
		await expect(page.locator('.assistant-msg .typewriter-text')).toContainText('Hi', { timeout: 20_000 });
		await expect(page.locator('.typewriter-text')).toHaveCount(0, { timeout: 8_000 });
		await waitForWsConnected(page);
		await expect(page.getByText('FIRST_RUN_MOCK_REPLY', { exact: true })).toBeAttached({ timeout: 25_000 });
		// Optional evidence frames at fixed times after send; the assertion alone runs in CI.
		const dir = process.env.FIRST_SEND_FRAMES;
		const prefix = `${size.width}x${size.height}`;
		if (dir) await page.screenshot({ path: `${dir}/${prefix}-before.png` });
		await chatComposer(page).fill('Hello from a new person.');
		const sentAt = Date.now();
		await sendButton(page).click();
		for (const seconds of dir ? [3, 15, 25] : [3]) {
			await page.waitForTimeout(Math.max(0, sentAt + seconds * 1000 - Date.now()));
			if (dir) await page.screenshot({ path: `${dir}/${prefix}-${seconds}s.png` });
			if (seconds === 3) {
				await expect(page.getByText('Hello from a new person.', { exact: true })).toBeAttached();
				await expect(page.getByText('FIRST_SEND_MOCK_REPLY', { exact: true }).first()).toBeAttached();
				await inTranscript(page, '.message.user:has-text("Hello from a new person.")');
				await inTranscript(page, '.message.assistant:has-text("FIRST_SEND_MOCK_REPLY") >> nth=0');
			}
		}
	});
});
