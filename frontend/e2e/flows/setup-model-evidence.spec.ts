import { expect, test, type Locator, type Page } from '@playwright/test';

test.use({ video: 'on' });

type SetupState = 'downloading' | 'failed' | 'not-installed' | 'ready';

const states: readonly SetupState[] = ['downloading', 'failed', 'not-installed', 'ready'];
const surfaces = [
	['800x600-light', 800, 600, 'light'],
	['800x600-dark', 800, 600, 'dark'],
	['1280x900-light', 1280, 900, 'light'],
	['1280x900-dark', 1280, 900, 'dark'],
] as const;

function sourcesFor(state: SetupState) {
	switch (state) {
		case 'downloading':
			return [{ source: 'omlx', name: 'Local model', state: 'needs_setup', setup_downloading: true, download_percent: 42 }];
		case 'failed':
			return [{ source: 'omlx', name: 'Local model', state: 'needs_setup', setup_error: 'download failed' }];
		case 'not-installed':
			return [{ source: 'claude-code', name: 'Claude Code', state: 'not_installed' }];
		case 'ready':
			return [{ source: 'omlx', name: 'Local model', state: 'ready' }];
	}
}

async function serveSetupState(page: Page, state: SetupState) {
	await page.route('**/api/auth/status', async (route) => {
		await route.fulfill({ json: { has_users: false } });
	});
	await page.route('**/api/setup/status', async (route) => {
		await route.fulfill({ json: { apple_silicon: true, macos_supported: true, local_eligible: true, model_name: 'Qwen 3.8', model_size_gb: 30, work_folder: '~/CORSIA' } });
	});
	await page.route('**/api/setup/local-download', async (route) => {
		await route.fulfill({ json: { ok: true, in_progress: state === 'downloading' } });
	});
	await page.route('**/api/setup/first-account', async (route) => {
		await route.fulfill({ json: { token: 'setup-evidence-token', user: { id: 'setup-evidence', name: 'First Person' } } });
	});
	await page.route('**/api/providers/sources/recheck', async (route) => {
		await route.fulfill({ json: { sources: sourcesFor(state) } });
	});
	await page.route('**/api/providers/sources', async (route) => {
		await route.fulfill({ json: { sources: sourcesFor(state) } });
	});
}

async function waitForJobSettled(page: Page, job: Locator) {
	const duration = await job.evaluate((element) => {
		const token = getComputedStyle(element).getPropertyValue('--dur-standard').trim();
		const value = Number.parseFloat(token) || 0;
		return value * (token.endsWith('ms') ? 2 : 2_000);
	});
	await page.waitForTimeout(duration);
}

async function openModelStep(page: Page) {
	await page.goto('/');
	await page.getByRole('button', { name: 'Get started' }).click();
	await page.locator('input[autocomplete="name"]').fill('First Person');
	await page.locator('input[autocomplete="username"]').fill('first');
	await page.locator('input[autocomplete="new-password"]').nth(0).fill('correct-password');
	await page.locator('input[autocomplete="new-password"]').nth(1).fill('correct-password');
	await page.getByRole('button', { name: 'Continue' }).click();
	await expect(page.getByRole('heading', { name: 'Choose an AI model' })).toBeVisible();
}

async function assertSetupState(page: Page, state: SetupState) {
	const start = page.getByRole('button', { name: 'Start using CORSIA' });
	if (state === 'ready') {
		await expect(start).toBeEnabled();
		await expect(page.locator('.pane').getByText('Ready', { exact: true })).toBeVisible();
		return;
	}

	await expect(start).toBeDisabled();
	if (state === 'downloading') {
		await expect(page.getByText('Downloading in the background')).toBeVisible();
		const job = page.locator('.job-row');
		await waitForJobSettled(page, job);
		await expect(job).toHaveCSS('opacity', '1');
		await expect.poll(() => job.evaluate((element) => element.getBoundingClientRect().height)).toBeGreaterThan(0);
		await expect(page.getByLabel('Setup progress').getByText('42%', { exact: true })).toBeVisible();
	} else if (state === 'failed') {
		await expect(page.getByRole('button', { name: 'Try again' })).toBeVisible();
		await expect(page.getByLabel('Setup progress').getByText('Couldn’t download', { exact: true })).toBeVisible();
	} else {
		await expect(page.getByText('Not installed', { exact: true })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Check again' })).toBeVisible();
	}
}

test.describe('@surface:first-run-model-model-source evidence', () => {

	test('captures every truthful model-source state at the approved evidence viewports', async ({ page }, testInfo) => {
		test.setTimeout(120_000);
		for (const state of states) {
			await serveSetupState(page, state);
			for (const [surface, width, height, colorScheme] of surfaces) {
				await page.setViewportSize({ width, height });
				await page.emulateMedia({ colorScheme, reducedMotion: 'reduce' });
				await openModelStep(page);
				await assertSetupState(page, state);
				await page.screenshot({ path: testInfo.outputPath(`setup-model-${state}-${surface}.png`), animations: 'disabled', scale: 'css' });
			}
		}
	});

	test('records Get started moving the local download into the background rail', async ({ page }, testInfo) => {
		await serveSetupState(page, 'downloading');
		await page.setViewportSize({ width: 1280, height: 900 });
		await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
		await page.goto('/');
		await page.getByRole('button', { name: 'Get started' }).click();
		await expect(page.getByRole('heading', { name: 'Create your account' })).toBeVisible();
		const job = page.locator('.job-row');
		await waitForJobSettled(page, job);
		await expect(job).toHaveCSS('opacity', '1');
		await expect.poll(() => job.evaluate((element) => element.getBoundingClientRect().height)).toBeGreaterThan(0);
		await expect(job.getByText('Qwen 3.8', { exact: true })).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath('setup-model-get-started-background-job-light-1280x900.png'), scale: 'css' });
	});

	test('captures a settled visible job row with animations enabled', async ({ page }, testInfo) => {
		await serveSetupState(page, 'downloading');
		await page.setViewportSize({ width: 1280, height: 900 });
		await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
		await openModelStep(page);
		const job = page.locator('.job-row');
		await waitForJobSettled(page, job);
		await expect(job).toHaveCSS('opacity', '1');
		await expect.poll(() => job.evaluate((element) => element.getBoundingClientRect().height)).toBeGreaterThan(0);
		await page.screenshot({ path: testInfo.outputPath('setup-model-downloading-settled-light-1280x900.png'), scale: 'css' });
	});

	test('records the job-rail entrance and button press in normal and reduced motion', async ({ page }) => {
		test.setTimeout(60_000);
		await serveSetupState(page, 'downloading');
		await page.setViewportSize({ width: 1280, height: 900 });

		await page.emulateMedia({ reducedMotion: 'no-preference' });
		await openModelStep(page);
		const normalAction = page.getByRole('button', { name: 'Use an API key instead' });
		await normalAction.hover();
		await page.mouse.down();
		await page.waitForTimeout(50);
		await expect.poll(() => normalAction.evaluate((element) => getComputedStyle(element).transform)).not.toBe('none');
		await page.mouse.up();

		await page.emulateMedia({ reducedMotion: 'reduce' });
		await openModelStep(page);
		const reducedAction = page.getByRole('button', { name: 'Use an API key instead' });
		await reducedAction.hover();
		await page.mouse.down();
		await expect(reducedAction.evaluate((element) => getComputedStyle(element).transform)).resolves.toBe('none');
		await page.mouse.up();
	});
});
