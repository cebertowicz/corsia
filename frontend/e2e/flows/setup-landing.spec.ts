import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { chatComposer, createSession, lastAssistantMessage, sendChatMessage, TEST_SESSION_TOKEN, waitForWsConnected } from '../support/corsia';
import { seedMockResponses } from '../support/mock';

// What is mocked: the setup routes only (/api/auth/status, /api/setup/status,
// /api/setup/local-model, /api/setup/local-download, /api/setup/first-account,
// /api/providers/sources*),
// so the screen is reachable on a workspace that already has an account and each
// model source can be shown ready. What is real: the signed-in app shell, conductor
// landing, thread, composer, websocket and the mocked-provider turn all run against
// the disposable e2e backend. The first-account route hands back the disposable
// session credential so the real shell can authenticate behind the mocked setup.

const SCREENSHOT_DIR = process.env.SETUP_LANDING_SHOTS_DIR;

const readySources = [
	['Claude Code', [{ source: 'claude-code', name: 'Claude Code', state: 'ready' }]],
	['This computer', [{ source: 'omlx', name: 'Local model', state: 'ready' }]],
] as const;

async function serveReadySetup(page: Page, request: APIRequestContext, sources: readonly Record<string, unknown>[]) {
	await page.route('**/api/auth/status', (route) => route.fulfill({ json: { has_users: false } }));
	await page.route('**/api/setup/status', (route) => route.fulfill({ json: { apple_silicon: true, macos_supported: true, local_eligible: true, work_folder: '~/CORSIA' } }));
	await page.route('**/api/setup/local-model', (route) => route.fulfill({ json: { state: 'picked', model: { name: 'Qwen 3.8', repository: 'example/qwen', download_size_bytes: 30_000_000_000, download_size_gb: 30, context_window: 65536 }, message: null } }));
	await page.route('**/api/setup/local-download', (route) => route.fulfill({ json: { ok: true, in_progress: false } }));
	await page.route('**/api/setup/first-account', (route) => route.fulfill({ json: { token: TEST_SESSION_TOKEN, user: { id: 'e2e', name: 'First Person' } } }));
	// A new person's workspace reports first_run_pending and names its CORSIA agent; earlier specs on the shared
	// backend have used the flag up and may have retired the agent, so force the flag on the real response and
	// name a fresh thread when none is left. The rest of the conductor state stays live.
	let landingThread: string | null = null;
	await page.route((url) => url.pathname === '/api/corsia-agent', async (route) => {
		if (route.request().method() !== 'GET') return route.continue();
		const response = await route.fetch();
		const body = await response.json();
		if (!body.target?.thread_id) landingThread ??= await createSession(request, 'CORSIA');
		const target = body.target?.thread_id ? body.target : { thread_id: landingThread };
		await route.fulfill({ response, json: { ...body, target, first_run_pending: true } });
	});
	await page.route('**/api/providers/sources/recheck', (route) => route.fulfill({ json: { sources } }));
	await page.route('**/api/providers/sources', (route) => route.fulfill({ json: { sources } }));
}

async function shot(page: Page, name: string) {
	if (SCREENSHOT_DIR) await page.screenshot({ path: `${SCREENSHOT_DIR}/${name}.png`, animations: 'disabled', scale: 'css' });
}

test.describe('@surface:first-run setup landing', () => {
	for (const [label, sources] of readySources) {
		test(`${label} ready: Start using CORSIA lands in a usable chat and a turn renders with attribution`, async ({ page, request }) => {
			await serveReadySetup(page, request, sources);
			const slug = label.toLowerCase().replace(/\s+/g, '-');
			const project = test.info().project.name;

			await page.goto('/');
			await page.getByRole('button', { name: 'Get started' }).click();
			await page.locator('input[autocomplete="name"]').fill('First Person');
			await page.locator('input[autocomplete="username"]').fill('first');
			await page.locator('input[autocomplete="new-password"]').nth(0).fill('correct-password');
			await page.locator('input[autocomplete="new-password"]').nth(1).fill('correct-password');
			await page.getByRole('button', { name: 'Continue' }).click();
			// SetupScreen.svelte: the model step is titled "Connect an AI".
			await expect(page.getByRole('heading', { name: 'Connect an AI' })).toBeVisible();
			const start = page.getByRole('button', { name: 'Start using CORSIA' });
			await expect(start).toBeEnabled();
			await start.click();

			// Landing: the setup flow is gone and the real app shell is up with a usable composer.
			await expect(page.getByRole('heading', { name: 'Connect an AI' })).toHaveCount(0);
			await expect(page.getByRole('button', { name: 'Start using CORSIA' })).toHaveCount(0);
			await expect(page.getByRole('button', { name: 'Get started' })).toHaveCount(0);
			await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible({ timeout: 20_000 });
			const composer = chatComposer(page);
			await expect(composer).toBeVisible({ timeout: 20_000 });
			await expect(composer).toBeEnabled();
			await expect(page.locator('.auth-gate')).toHaveCount(0);
			// The CORSIA agent opens with a model chosen (not the 'Select Model' placeholder) and a live socket.
			await waitForWsConnected(page);
			await shot(page, `landing-${slug}-${project}`);

			// One send through the mocked provider renders a reply with its model badge.
			await seedMockResponses(request, [{ content: 'SETUP_LANDING_REPLY', finish_reason: 'stop' }]);
			await sendChatMessage(page, 'Say hello.');
			// Focus is on by default and tucks finished work behind a summary; open it to read the reply itself.
			const reply = lastAssistantMessage(page);
			await expect(async () => {
				const worked = page.getByRole('button', { name: /^Expand Worked/ });
				if (await worked.isVisible()) await worked.click();
				await expect(reply).toContainText('SETUP_LANDING_REPLY', { timeout: 2_000 });
			}).toPass({ timeout: 25_000 });
			const badge = reply.locator('.msg-model-badge');
			await expect(badge).toBeVisible();
			await expect(badge).not.toHaveText('');
			await shot(page, `reply-${slug}-${project}`);
		});
	}
});
