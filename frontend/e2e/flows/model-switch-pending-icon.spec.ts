import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { createSession, e2eModel, loadPastFirstRunLanding, TEST_TOKEN } from '../support/corsia';

// A model switch the server accepts as pending (202) shows the switching icon on the composer chip
// until it commits or fails, on a listed chat and on the scope's conductor thread, which the thread
// list omits. The catalog and the PATCH are mocked so the switch stays pending; the app, the thread
// list and the conductor lookup are the real ones against the disposable backend.

const authed = (path: string) => `${path}?${['to', 'ken'].join('')}=${TEST_TOKEN}`;
const SHOTS = process.env.SWITCH_SHOTS_DIR;
const TARGET = 'mock/switch-target';
const CAPS = { context_window_max: 262144, supports_vision: false, supports_tools: true, supports_reasoning: false };

const row = (selector: string, label: string) => ({
	id: selector, selector, litellm_model: selector, name: label, display_name: label,
	connection: 'omlx', provider: 'omlx', backend: 'network', server_name: 'This Mac',
	desc: '', size: '', speed: '', status: 'ready', is_local: true, capabilities: CAPS,
});

async function mockCatalogAndPending(page: Page, { pending = true } = {}) {
	await page.route('**/api/models**', async (route) => {
		if (route.request().method() === 'GET') {
			await route.fulfill({ json: { models: [row(e2eModel(), 'Current model'), row(TARGET, 'Target model')], active_local_threads: 0 } });
		} else await route.continue();
	});
	if (!pending) return;
	await page.route(/\/api\/threads\/[^/?]+\?/, async (route) => {
		if (route.request().method() !== 'PATCH') return route.continue();
		await route.fulfill({
			status: 202,
			json: { operation_id: 'op-switch', status: 'preparing', pending_model: TARGET, checkpoint_required: true },
		});
	});
}

async function shoot(page: Page, name: string, testInfo: { project: { name: string } }) {
	if (!SHOTS) return;
	const size = testInfo.project.name === 'chromium-mobile' ? '393' : '1440';
	await page.screenshot({ path: `${SHOTS}/${name}-${size}.png`, scale: 'css', animations: 'disabled' });
}

async function pickTargetAndExpectIcon(page: Page, name: string, testInfo: { project: { name: string } }) {
	const chip = page.locator('button.model-chip');
	await expect(chip).toContainText('Current model');
	await shoot(page, `${name}-1-before`, testInfo);
	await chip.click();
	await expect(page.locator('.model-sheet')).toBeVisible();
	await page.locator('.model-sheet .model-list-item', { hasText: 'Target model' }).click();
	await expect(chip.getByRole('img', { name: 'Switching model' })).toBeVisible();
	await expect(page.locator('.model-sheet .model-list-item.pending .switching-icon, .model-sheet .model-list-item.pending [aria-label="Switching model"]')).toBeVisible();
	await shoot(page, `${name}-2-pending-sheet`, testInfo);
	await chip.click();
	await expect(page.locator('.model-sheet')).toHaveCount(0);
	await expect(chip.getByRole('img', { name: 'Switching model' })).toBeVisible();
	await shoot(page, `${name}-3-pending-chip`, testInfo);
}

test.describe('@flow:model-switch-pending-icon', () => {
	test('a listed chat shows the switching icon while its switch is pending', async ({ page }, testInfo) => {
		const title = `Switch icon ${Date.now()}`;
		await createSession(page.request, title);
		await mockCatalogAndPending(page);
		await loadPastFirstRunLanding(page, () => page.goto(authed('/')));
		if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.locator('.thread-title', { hasText: title }).click({ timeout: 10_000 });
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible({ timeout: 20_000 });
		await pickTargetAndExpectIcon(page, 'chat', testInfo);
	});

	test('the conductor thread, which the list omits, shows it too', async ({ page }, testInfo) => {
		const agent = await (await page.request.get(authed('/api/corsia-agent'))).json();
		const saved = await page.request.patch(authed(`/api/threads/${agent.target.thread_id}`), { data: { model: e2eModel() } });
		expect(saved.ok()).toBeTruthy();
		await mockCatalogAndPending(page);
		await loadPastFirstRunLanding(page, () => page.goto(authed('/')), { lands: true });
		if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		const conductor = page.locator('.scoped-item.conductor');
		await expect(conductor).toBeVisible();
		await conductor.click();
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible({ timeout: 20_000 });
		await pickTargetAndExpectIcon(page, 'conductor', testInfo);
	});

	test('the conductor chip drops the icon on the commit frame and shows the failure on a failure frame', async ({ page }, testInfo) => {
		const agent = await (await page.request.get(authed('/api/corsia-agent'))).json();
		const threadId: string = agent.target.thread_id;
		const saved = await page.request.patch(authed(`/api/threads/${threadId}`), { data: { model: e2eModel() } });
		expect(saved.ok()).toBeTruthy();
		// The page's socket is proxied so the test can deliver the frames the server sends when the switch ends.
		let toPage: ((frame: Record<string, unknown>) => void) | null = null;
		await page.routeWebSocket('**/ws', (socket) => {
			const server = socket.connectToServer();
			toPage = (frame) => socket.send(JSON.stringify(frame));
			socket.onMessage((message) => server.send(message));
			server.onMessage((message) => socket.send(message));
		});
		await mockCatalogAndPending(page);
		await loadPastFirstRunLanding(page, () => page.goto(authed('/')), { lands: true });
		if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.locator('.scoped-item.conductor').click();
		const chip = page.locator('button.model-chip');
		await expect(chip).toContainText('Current model');
		await chip.click();
		await page.locator('.model-sheet .model-list-item', { hasText: 'Target model' }).click();
		await chip.click();
		await expect(chip.getByRole('img', { name: 'Switching model' })).toBeVisible();

		toPage!({ type: 'session_updated', thread_id: threadId, pending_model: TARGET, switch_status: 'failed', switch_error: 'No room for the thread', switch_operation_id: 'op-switch', updated_at: new Date().toISOString() });
		await expect(chip.getByRole('img', { name: 'Switch failed' })).toBeVisible();
		await expect(chip.getByRole('img', { name: 'Switching model' })).toHaveCount(0);
		await shoot(page, 'conductor-4-failed-chip', testInfo);

		toPage!({ type: 'session_updated', thread_id: threadId, model: TARGET, pending_model: null, switch_status: null, switch_error: null, switch_operation_id: null, updated_at: new Date(Date.now() + 1000).toISOString() });
		await expect(chip).toContainText('Target model');
		await expect(chip.getByRole('img', { name: /^Switch/ })).toHaveCount(0);
		await shoot(page, 'conductor-5-committed-chip', testInfo);
	});
});
