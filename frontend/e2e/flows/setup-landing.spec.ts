import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { chatComposer, lastAssistantMessage, sendChatMessage, TEST_SESSION_TOKEN, waitForWsConnected } from '../support/corsia';
import { seedMockResponses } from '../support/mock';
import { execFile } from 'node:child_process';
import { resolve } from 'node:path';
import { promisify } from 'node:util';

// What is mocked: the setup routes only (/api/auth/status, /api/setup/status,
// /api/setup/local-model, /api/setup/local-download, /api/setup/first-account,
// /api/providers/sources*) and the model catalog the picker reads,
// so the screen is reachable on a workspace that already has an account and each
// model source can be shown ready. What is real: the signed-in app shell, conductor
// landing, thread, composer, websocket and the mocked-provider turn all run against
// the disposable e2e backend. The first-account route hands back the disposable
// session credential so the real shell can authenticate behind the mocked setup.

const SCREENSHOT_DIR = process.env.SETUP_LANDING_SHOTS_DIR;
const seedAppLandingProcess = promisify(execFile);
const APP_LANDING_SEED_TIMEOUT_MS = 15_000;
// The packaged seeded opening reveals over 1.8s; this is its full E2E budget,
// including the post-picker render and the slower mobile renderer.
const SEEDED_OPENING_REVEAL_BUDGET_MS = 8_000;

async function seedAppLanding() {
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (!python || !workspace || !state) throw new Error('setup landing requires the disposable e2e-local environment');
	// Convert only the harness's unused CLI seed into an app landing. No greeting
	// is seeded: the real first-model transaction must create and broadcast it.
	await seedAppLandingProcess(python, ['-c', [
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
	].join('\n')], {
		cwd: resolve('..', 'backend'),
		env: { ...process.env, CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state, CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1' },
		timeout: APP_LANDING_SEED_TIMEOUT_MS,
		killSignal: 'SIGTERM',
		maxBuffer: 64 * 1024,
	});
}

const readySources = [
	['Claude Code', [{ source: 'claude-code', name: 'Claude Code', state: 'ready' }]],
	['This computer', [{ source: 'omlx', name: 'Local model', state: 'ready' }]],
] as const;

async function serveReadySetup(page: Page, sources: readonly Record<string, unknown>[]) {
	await page.route('**/api/auth/status', (route) => route.fulfill({ json: { has_users: false } }));
	await page.route('**/api/setup/status', (route) => route.fulfill({ json: { apple_silicon: true, macos_supported: true, local_eligible: true, work_folder: '~/CORSIA' } }));
	await page.route('**/api/setup/progress', (route) => route.fulfill({ json: { setup_completed: false, step: 'models', local_ai: false } }));
	const isClaude = sources[0]?.source === 'claude-code';
	const selectedModel = 'mock/test-model';
	await page.route((url) => url.pathname === '/api/models', (route) => route.request().method() === 'GET' ? route.fulfill({ json: { models: [isClaude
		? { id: selectedModel, litellm_model: selectedModel, name: 'Vendor default', connection: 'claude-code', provider: 'claude-code', backend: 'api', status: 'ready', available: true, is_default: true, runs_on: [{ account_id: 'disposable-account', selected: true, status: 'ready' }] }
		: { id: selectedModel, litellm_model: selectedModel, name: 'Vendor default', connection: 'omlx', provider: 'omlx', backend: 'network', status: 'ready', is_default: true }], active_local_threads: 0 } }) : route.continue());
	await page.route('**/api/setup/local-model', (route) => route.fulfill({ json: { state: 'picked', model: { name: 'Qwen 3.8', repository: 'example/qwen', download_size_bytes: 30_000_000_000, download_size_gb: 30, context_window: 65536 }, message: null } }));
	await page.route('**/api/setup/local-download', (route) => route.fulfill({ json: { ok: true, in_progress: true } }));
	await page.route('**/api/setup/first-account', (route) => route.fulfill({ json: { token: TEST_SESSION_TOKEN, user: { id: 'e2e', name: 'First Person' } } }));
	await page.route('**/api/providers/sources/recheck**', (route) => route.fulfill({ json: { sources } }));
	await page.route('**/api/providers/sources**', (route) => route.fulfill({ json: { sources } }));
}

// The greeting begins inside the visible transcript viewport: below the rendered
// glass header and above the floating composer when it overlaps the scroller.
async function expectGreetingFirstLineVisible(page: Page) {
	const firstLine = page.locator('.assistant-msg').getByText('Hi First.', { exact: false }).and(page.locator(':not(.sr-only)')).first();
	await expect(firstLine).toBeVisible();
	await expect(async () => {
		const [line, header, transcript, composer] = await Promise.all([
			firstLine.boundingBox(),
			page.locator('.shell-header').boundingBox(),
			page.locator('.messages').boundingBox(),
			page.locator('.input-floater').boundingBox(),
		]);
		const viewportHeight = await page.evaluate(() => window.innerHeight);
		expect(line && header && transcript).toBeTruthy();
		const composerOverlapsTranscript = composer
			&& composer.y < transcript!.y + transcript!.height
			&& composer.y + composer.height > transcript!.y;
		const visibleBottom = Math.min(
			viewportHeight,
			transcript!.y + transcript!.height,
			composerOverlapsTranscript ? composer!.y : Infinity,
		);
		expect(line!.y).toBeGreaterThanOrEqual(header!.y + header!.height);
		expect(line!.y + line!.height).toBeLessThanOrEqual(visibleBottom);
	}).toPass({ timeout: 2_000 });
}

async function shot(page: Page, name: string) {
	if (SCREENSHOT_DIR) await page.screenshot({ path: `${SCREENSHOT_DIR}/${name}.png`, animations: 'disabled', scale: 'css' });
}

test.describe('@surface:first-run setup landing', () => {
	// Each test ends the first run this screen is for, so each starts from the seed, with it pending.
	test.use({ seedPerTest: true });
	// Backend reset, Python seeding, setup and two turns share the test budget.
	test.slow();
	for (const [label, sources] of readySources) {
		test(`${label} ready: Start using CORSIA lands in a usable chat and a turn renders with attribution`, async ({ page, request }) => {
			await seedAppLanding();
			await serveReadySetup(page, sources);
			const slug = label.toLowerCase().replace(/\s+/g, '-');
			const project = test.info().project.name;
			if (project === 'chromium-mobile') await page.setViewportSize({ width: 393, height: 852 });

			const appSocket = page.waitForEvent('websocket', {
				predicate: (socket) => new URL(socket.url()).pathname === '/ws',
				timeout: 20_000,
			});
			await page.goto('/');
			await expect(page.getByRole('heading', { name: 'Welcome to CORSIA' })).toBeVisible();
			await page.getByRole('button', { name: 'Continue' }).click();
			await page.locator('input[autocomplete="name"]').fill('First Person');
			await page.locator('input[autocomplete="username"]').fill('first');
			await page.locator('input[autocomplete="new-password"]').nth(0).fill('correct-password');
			await page.locator('input[autocomplete="new-password"]').nth(1).fill('correct-password');
			await page.getByRole('button', { name: 'Continue' }).click();
			// With a tool not signed in the subscriptions question comes first: answer it without picking a plan.
			await expect(page.getByRole('heading', { name: 'Your subscriptions' })).toBeVisible();
			await page.getByRole('button', { name: /^(No|Neither)$/ }).click();
			await expect(page.getByRole('heading', { name: 'Providers & models' })).toBeVisible();
			const start = page.getByRole('button', { name: 'Start using CORSIA' });
			await expect(start).toBeEnabled();
			await start.click();

			// Landing: the setup flow is gone and the real app shell is up with a usable composer.
			await expect(page.getByRole('heading', { name: 'Providers & models' })).toHaveCount(0);
			await expect(page.getByRole('button', { name: 'Start using CORSIA' })).toHaveCount(0);
			await expect(page.getByRole('button', { name: 'Get started' })).toHaveCount(0);
			await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible({ timeout: 20_000 });
			const composer = chatComposer(page);
			await expect(composer).toBeVisible({ timeout: 20_000 });
			await expect(composer).toBeEnabled();
			await expect(page.locator('.auth-gate')).toHaveCount(0);
			await appSocket;
			// Setup chose no model: the CORSIA agent waits on the person's pick, and the first-run message is held until then.
			const chip = page.locator('button.model-chip');
			await expect(chip).toContainText('Select Model');
			await expect(page.getByText('Hi First.', { exact: false })).toHaveCount(0);
			const planChip = page.getByRole('button', { name: /^Open plan —/ });
			if (project === 'chromium-mobile') {
				await expect(planChip).toBeVisible();
				const box = await planChip.boundingBox();
				expect(box!.y).toBeGreaterThan(page.viewportSize()!.height / 2);
			} else {
				await expect(planChip).toHaveCount(0);
			}
			await shot(page, `landing-${slug}-${project}`);

			// The first-run message and the person's first send reach the mocked provider in that order.
			await seedMockResponses(request, [
				{ content: 'SETUP_LANDING_FIRST_RUN_REPLY', finish_reason: 'stop' },
				{ content: 'SETUP_LANDING_REPLY', finish_reason: 'stop' },
			]);
			await chip.click();
			const pickedAt = Date.now();
			await page.locator('button.model-list-item', { hasText: 'Vendor default' }).click();
			const opening = page.locator('.assistant-msg').filter({ has: page.locator('.typewriter-text') });
			await expect(opening.locator('.typewriter-text')).toContainText('Hi', { timeout: 2_000 });
			expect(Date.now() - pickedAt).toBeLessThan(2_500);
			await expect(page.getByText('SETUP_LANDING_FIRST_RUN_REPLY', { exact: true })).toHaveCount(0);
			await expect(page.locator('[aria-live="polite"][aria-atomic="true"]')).toContainText("I'm CORSIA.");
			await shot(page, `opening-live-${slug}-${project}`);
			// This seeded opening is 1.2k characters; its acceleration crosses the
			// original two-second observation window under a mobile renderer.
			await expect(page.locator('.typewriter-text')).toHaveCount(0, { timeout: SEEDED_OPENING_REVEAL_BUDGET_MS });
			await expectGreetingFirstLineVisible(page);
			await shot(page, `opening-complete-${slug}-${project}`);
			await expect(chip).toContainText('Vendor default');
			await waitForWsConnected(page);
			// Focus is on by default and tucks finished work behind a summary; open it to read a reply itself.
			const reply = lastAssistantMessage(page);
			const readReply = async (text: string) => expect(async () => {
				const worked = page.getByRole('button', { name: /^Expand Worked/ }).last();
				if (await worked.isVisible()) await worked.click();
				await expect(reply).toContainText(text, { timeout: 2_000 });
			}).toPass({ timeout: 25_000 });
			// Picking the model starts the held first-run message.
			await readReply('SETUP_LANDING_FIRST_RUN_REPLY');
			await page.unroute('**/api/auth/status');
			await page.unroute('**/api/setup/progress');
			await page.reload();
			await expect(page.locator('.typewriter-text')).toHaveCount(0);
			await expect(page.getByText('Hi First.', { exact: false })).toBeVisible();
			await waitForWsConnected(page);
			await sendChatMessage(page, 'Say hello.');
			await readReply('SETUP_LANDING_REPLY');
			const badge = reply.locator('.msg-model-badge');
			await expect(badge).toBeVisible();
			await expect(badge).not.toHaveText('');
			await shot(page, `reply-${slug}-${project}`);
		});
	}
});
