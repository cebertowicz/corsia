import { expect, test, type Locator, type Page } from '@playwright/test';

test.use({ video: { mode: 'on', size: { width: 1280, height: 900 } } });

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

async function serveSetupState(page: Page, state: SetupState | (() => SetupState)) {
	const currentState = () => typeof state === 'function' ? state() : state;
	await page.route('**/api/auth/status', async (route) => {
		await route.fulfill({ json: { has_users: false } });
	});
	await page.route('**/api/setup/status', async (route) => {
		await route.fulfill({ json: { apple_silicon: true, macos_supported: true, local_eligible: true, model_name: 'Qwen 3.8', model_size_gb: 30, work_folder: '~/CORSIA' } });
	});
	await page.route('**/api/setup/local-download', async (route) => {
		await route.fulfill({ json: { ok: true, in_progress: currentState() === 'downloading' } });
	});
	await page.route('**/api/setup/first-account', async (route) => {
		await route.fulfill({ json: { token: 'setup-evidence-token', user: { id: 'setup-evidence', name: 'First Person' } } });
	});
	await page.route('**/api/providers/sources/recheck', async (route) => {
		await route.fulfill({ json: { sources: sourcesFor(currentState()) } });
	});
	await page.route('**/api/providers/sources', async (route) => {
		await route.fulfill({ json: { sources: sourcesFor(currentState()) } });
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

async function assertDoneChecksSettled(page: Page) {
	const checks = page.locator('.rail .row.done .done-check path');
	await expect(checks).toHaveCount(2);
	await expect.poll(() => checks.evaluateAll((paths) => paths.every((path) => Number.parseFloat(getComputedStyle(path).strokeDashoffset) === 0))).toBe(true);
}

async function waitForRailAnimationsSettled(page: Page) {
	const rail = page.locator('.rail');
	await expect.poll(() => rail.evaluate((element) => Array.from(element.querySelectorAll<HTMLElement>('.job-region, .job-row')).flatMap((node) => node.getAnimations()).filter((animation) => animation.playState === 'running').length)).toBe(0);
}

async function waitForPaneAnimationsSettled(page: Page) {
	const pane = page.locator('.pane');
	await expect.poll(() => pane.evaluate((element) => element.getAnimations({ subtree: true }).filter((animation) => animation.playState === 'running').length)).toBe(0);
}

async function paneCrossfade(page: Page) {
	return page.locator('.pane').evaluate((pane) => {
		const scenes = Array.from(pane.querySelectorAll<HTMLElement>('.pane-scene'));
		return scenes.map((scene) => {
			const box = scene.getBoundingClientRect();
			const footer = scene.querySelector('footer')?.getBoundingClientRect();
			return {
				title: scene.querySelector('h1')?.textContent,
				opacity: Number.parseFloat(getComputedStyle(scene).opacity),
				transform: getComputedStyle(scene).transform,
				inert: scene.inert,
				box: { x: box.x, y: box.y, width: box.width, height: box.height },
				footerY: footer?.y,
			};
		});
	});
}

async function waitForPaneCrossfade(page: Page) {
	await expect.poll(async () => {
		const scenes = await paneCrossfade(page);
		return scenes.length === 2 && scenes.every((scene) => scene.opacity > 0 && scene.opacity < 1);
	}).toBe(true);
	return paneCrossfade(page);
}

type FooterSnapshot = {
	footerCount: number;
	pillCount: number;
	button: { x: number; y: number; width: number; height: number } | null;
	labels: { text: string; x: number; y: number; width: number; height: number; opacity: number }[];
};

async function setupFooter(page: Page): Promise<FooterSnapshot> {
	return page.locator('.pane > footer').evaluate((footer) => {
		const box = (element: Element) => {
			const rect = element.getBoundingClientRect();
			return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
		};
		const button = footer.querySelector<HTMLButtonElement>('.btn.pill');
		return {
			footerCount: document.querySelectorAll('.pane > footer').length,
			pillCount: footer.querySelectorAll('.btn.pill').length,
			button: button ? box(button) : null,
			labels: Array.from(footer.querySelectorAll<HTMLElement>('.primary-label')).map((label) => ({ text: label.textContent ?? '', ...box(label), opacity: Number.parseFloat(getComputedStyle(label).opacity) })),
		};
	});
}

function expectSinglePersistentPill(footer: FooterSnapshot, labels: string[]) {
	expect(footer.footerCount).toBe(1);
	expect(footer.pillCount).toBe(1);
	expect(footer.button).not.toBeNull();
	expect(footer.labels.map((label) => label.text).sort()).toEqual(labels.sort());
	for (const label of footer.labels) {
		expect(label.width).toBeGreaterThan(0);
		expect(label.height).toBeGreaterThan(0);
	}
	if (footer.labels.length > 1) {
		const [first, ...rest] = footer.labels;
		for (const label of rest) {
			expect(label).toMatchObject({ x: first.x, y: first.y, width: first.width, height: first.height });
		}
		expect(footer.labels.reduce((opacity, label) => opacity + label.opacity, 0)).toBeCloseTo(1, 1);
	}
}

async function openModelStep(page: Page) {
	await page.addInitScript(() => localStorage.setItem('corsia_theme', 'auto'));
	await page.goto('/');
	await waitForPaneAnimationsSettled(page);
	await page.getByRole('button', { name: 'Get started' }).click();
	await page.locator('input[autocomplete="name"]').fill('First Person');
	await page.locator('input[autocomplete="username"]').fill('first');
	await page.locator('input[autocomplete="new-password"]').nth(0).fill('correct-password');
	await page.locator('input[autocomplete="new-password"]').nth(1).fill('correct-password');
	await page.getByRole('button', { name: 'Continue' }).click();
	await expect(page.getByRole('heading', { name: 'Choose an AI model' })).toBeVisible();
	await waitForPaneAnimationsSettled(page);
	await waitForRailAnimationsSettled(page);
}

async function assertSetupState(page: Page, state: SetupState) {
	await waitForPaneAnimationsSettled(page);
	const start = page.getByRole('button', { name: 'Start using CORSIA' });
	if (state === 'ready') {
		await expect(start).toBeEnabled();
		await expect(page.locator('.pane').getByText('Ready', { exact: true })).toBeVisible();
		return;
	}

	await expect(start).toBeDisabled();
	if (state === 'downloading') {
		await expect(page.getByText('Downloading in the background')).toBeVisible();
		const job = page.locator('.job-row:not([inert])');
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
				await expect(page.locator('html')).toHaveAttribute('data-theme', colorScheme);
				await assertSetupState(page, state);
				await page.screenshot({ path: testInfo.outputPath(`setup-model-${state}-${surface}.png`), animations: 'disabled', scale: 'css' });
			}
		}
	});

	test('records the setup model sequence from not installed through This computer ready', async ({ page }, testInfo) => {
		let state: SetupState = 'not-installed';
		await serveSetupState(page, () => state);
		await page.setViewportSize({ width: 1280, height: 900 });
		await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
		await openModelStep(page);
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
		await assertSetupState(page, 'not-installed');
		await page.screenshot({ path: testInfo.outputPath('setup-model-not-installed-light-1280x900.png'), scale: 'css' });

		state = 'downloading';
		await page.getByRole('button', { name: 'Check again' }).click();
		await expect(page.getByText('This computer', { exact: true })).toBeVisible();
		await assertSetupState(page, 'downloading');
		await page.screenshot({ path: testInfo.outputPath('setup-model-this-computer-downloading-light-1280x900.png'), scale: 'css' });

		state = 'ready';
		await page.evaluate(async () => {
			const events = await import('/src/lib/ws/events.ts');
			events.emit('models_updated', {});
		});
		await assertSetupState(page, 'ready');
		await waitForRailAnimationsSettled(page);
		await page.screenshot({ path: testInfo.outputPath('setup-model-this-computer-ready-light-1280x900.png'), scale: 'css' });
		await page.getByRole('button', { name: 'Start using CORSIA' }).click();
		await expect(page.getByRole('heading', { name: 'Choose an AI model' })).not.toBeVisible();
	});

	test('records Get started moving the local download into the background rail', async ({ page }, testInfo) => {
		await serveSetupState(page, 'downloading');
		await page.setViewportSize({ width: 1280, height: 900 });
		await page.addInitScript(() => localStorage.setItem('corsia_theme', 'auto'));
		await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
		await page.goto('/');
		await page.locator('html').evaluate((element) => {
			element.style.setProperty('--dur-standard', '3000ms');
		});
		const shell = page.locator('.setup-shell');
		await shell.evaluate((element) => element.setAttribute('data-persistence-probe', 'welcome'));
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
		await expect(page.getByLabel('Setup progress').getByText('Welcome', { exact: true }).locator('..').locator('..').locator('svg')).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath('setup-welcome-current-rail-light-1280x900.png'), scale: 'css' });
		await page.getByRole('button', { name: 'Get started' }).click();
		await expect(page.getByRole('heading', { name: 'Create your account' })).toBeVisible();
		await expect(shell).toHaveAttribute('data-persistence-probe', 'welcome');
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
		const job = page.locator('.job-row:not([inert])');
		const motion = await page.evaluate(async () => {
			await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
			const region = document.querySelector<HTMLElement>('.job-region');
			if (!region) throw new Error('Missing background-job region');
			const token = getComputedStyle(region).getPropertyValue('--dur-standard').trim();
			const duration = (Number.parseFloat(token) || 0) * (token.endsWith('ms') ? 1 : 1_000);
			const leadStateCount = document.querySelectorAll('.rail .lead-state').length;
			await new Promise<void>((resolve) => window.setTimeout(resolve, duration / 2));
			return { halfHeight: region.getBoundingClientRect().height, leadStateCount };
		});
		await page.screenshot({ path: testInfo.outputPath('setup-model-get-started-background-job-halfway-light-1280x900.png'), scale: 'css' });
		await waitForJobSettled(page, job);
		const fullHeight = await page.locator('.job-region').evaluate((element) => element.getBoundingClientRect().height);
		expect(motion.halfHeight).toBeGreaterThan(0);
		expect(motion.halfHeight).toBeLessThan(fullHeight);
		expect(motion.leadStateCount).toBeGreaterThan(3);
		await expect(job).toHaveCSS('opacity', '1');
		await expect.poll(() => job.evaluate((element) => element.getBoundingClientRect().height)).toBeGreaterThan(0);
		await expect(job.getByText('Qwen 3.8', { exact: true })).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath('setup-model-get-started-background-job-light-1280x900.png'), scale: 'css' });
	});

	test('records stacked, focused pane crossfades through setup', async ({ page }, testInfo) => {
		test.setTimeout(60_000);
		await serveSetupState(page, 'ready');
		await page.setViewportSize({ width: 1280, height: 900 });
		await page.addInitScript(() => localStorage.setItem('corsia_theme', 'auto'));
		await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
		await page.goto('/');
		await page.locator('html').evaluate((element) => element.style.setProperty('--dur-tight', '1000ms'));
		const welcome = page.getByRole('heading', { name: 'Welcome to CORSIA' });
		await expect(welcome).toBeVisible();
		await waitForPaneAnimationsSettled(page);
		const welcomePaintedAt = await page.evaluate(() => performance.now());
		await page.waitForTimeout(300);
		expect(await page.evaluate((start) => performance.now() - start, welcomePaintedAt)).toBeGreaterThanOrEqual(300);
		await page.screenshot({ path: testInfo.outputPath('setup-welcome-visible-before-start-light-1280x900.png'), scale: 'css' });
		const welcomeFooter = await setupFooter(page);
		expectSinglePersistentPill(welcomeFooter, ['Get started']);
		await page.getByRole('button', { name: 'Get started' }).click();
		const welcomeToAccount = await waitForPaneCrossfade(page);
		expect(welcomeToAccount).toHaveLength(2);
		expect(welcomeToAccount.map((scene) => scene.title).sort()).toEqual(['Create your account', 'Welcome to CORSIA']);
		for (const scene of welcomeToAccount) {
			expect(scene.opacity).toBeGreaterThan(0);
			expect(scene.opacity).toBeLessThan(1);
			expect(scene.box).toEqual(welcomeToAccount[0].box);
			expect(scene.footerY).toBe(welcomeToAccount[0].footerY);
		}
		expect(welcomeToAccount.find((scene) => scene.title === 'Welcome to CORSIA')?.inert).toBe(true);
		expect(welcomeToAccount.find((scene) => scene.title === 'Create your account')?.inert).toBe(false);
		const welcomeToAccountFooter = await setupFooter(page);
		expectSinglePersistentPill(welcomeToAccountFooter, ['Get started', 'Continue']);
		expect(welcomeToAccountFooter.button?.width).toBeGreaterThanOrEqual(welcomeFooter.button?.width ?? 0);
		await page.screenshot({ path: testInfo.outputPath('setup-welcome-account-mid-crossfade-light-1280x900.png'), scale: 'css' });
		await expect(page.getByRole('heading', { name: 'Create your account' })).toBeFocused();
		await waitForPaneAnimationsSettled(page);

		for (const [locator, value] of [
			[page.locator('input[autocomplete="name"]'), 'First Person'],
			[page.locator('input[autocomplete="username"]'), 'first'],
			[page.locator('input[autocomplete="new-password"]').nth(0), 'correct-password'],
			[page.locator('input[autocomplete="new-password"]').nth(1), 'correct-password'],
		] as const) await locator.fill(value);
		const accountFooter = await setupFooter(page);
		expectSinglePersistentPill(accountFooter, ['Continue']);
		await page.getByRole('button', { name: 'Continue' }).click();
		const accountToModels = await waitForPaneCrossfade(page);
		expect(accountToModels).toHaveLength(2);
		expect(accountToModels.map((scene) => scene.title).sort()).toEqual(['Choose an AI model', 'Create your account']);
		for (const scene of accountToModels) {
			expect(scene.opacity).toBeGreaterThan(0);
			expect(scene.opacity).toBeLessThan(1);
			expect(scene.box).toEqual(accountToModels[0].box);
			expect(scene.footerY).toBe(accountToModels[0].footerY);
		}
		expect(accountToModels.find((scene) => scene.title === 'Create your account')?.inert).toBe(true);
		expect(accountToModels.find((scene) => scene.title === 'Choose an AI model')?.inert).toBe(false);
		const accountToModelsFooter = await setupFooter(page);
		expectSinglePersistentPill(accountToModelsFooter, ['Continue', 'Start using CORSIA']);
		expect(accountToModelsFooter.button?.width).toBeGreaterThanOrEqual(accountFooter.button?.width ?? 0);
		await page.screenshot({ path: testInfo.outputPath('setup-account-models-mid-crossfade-light-1280x900.png'), scale: 'css' });
		await expect(page.getByRole('heading', { name: 'Choose an AI model' })).toBeFocused();
	});

	test('captures the Welcome setup screen in dark mode', async ({ page }, testInfo) => {
		await serveSetupState(page, 'ready');
		await page.setViewportSize({ width: 1280, height: 900 });
		await page.addInitScript(() => localStorage.setItem('corsia_theme', 'auto'));
		await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'no-preference' });
		await page.goto('/');
		await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
		await expect(page.getByRole('heading', { name: 'Welcome to CORSIA' })).toBeVisible();
		await waitForPaneAnimationsSettled(page);
		expectSinglePersistentPill(await setupFooter(page), ['Get started']);
		await page.screenshot({ path: testInfo.outputPath('setup-welcome-visible-dark-1280x900.png'), scale: 'css' });
	});

	test('keeps reduced-motion setup changes as fade-only pane crossfades', async ({ page }) => {
		await serveSetupState(page, 'ready');
		await page.setViewportSize({ width: 1280, height: 900 });
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await page.goto('/');
		await page.locator('html').evaluate((element) => element.style.setProperty('--dur-tight', '1000ms'));
		await expect(page.getByRole('heading', { name: 'Welcome to CORSIA' })).toBeVisible();
		await waitForPaneAnimationsSettled(page);
		await page.getByRole('button', { name: 'Get started' }).click();
		const scenes = await waitForPaneCrossfade(page);
		expect(scenes).toHaveLength(2);
		for (const scene of scenes) {
			expect(scene.opacity).toBeGreaterThan(0);
			expect(scene.opacity).toBeLessThan(1);
			expect(scene.transform).toBe('none');
			expect(scene.box).toEqual(scenes[0].box);
		}
		await expect(page.getByRole('heading', { name: 'Create your account' })).toBeFocused();
	});

	test('captures a settled visible job row with animations enabled', async ({ page }, testInfo) => {
		await serveSetupState(page, 'downloading');
		await page.setViewportSize({ width: 1280, height: 900 });
		await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
		await openModelStep(page);
		const job = page.locator('.job-row:not([inert])');
		await waitForJobSettled(page, job);
		await assertDoneChecksSettled(page);
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
