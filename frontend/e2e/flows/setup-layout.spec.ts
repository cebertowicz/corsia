import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { TEST_SESSION_TOKEN } from '../support/corsia';

// Every setup screen at the setup floor, a mid width and the common desktop sizes: from 1280 wide the logo sits in the
// right-hand brand column and no text or control touches it; below 1280 there is no brand column. What is mocked: the
// setup routes only, as in setup-landing.spec.ts. Set SETUP_LAYOUT_SHOTS_DIR to also save a screenshot of each screen.

const SHOTS = process.env.SETUP_LAYOUT_SHOTS_DIR;
const SIZES = [[800, 600], [1000, 700], [1280, 600], [1280, 800], [1728, 1000]] as const;
const notInstalled = [{ source: 'claude-code', name: 'Claude Code', state: 'not_installed' }, { source: 'codex', name: 'Codex', state: 'not_installed' }];
const ready = [{ source: 'claude-code', name: 'Claude Code', state: 'ready' }, { source: 'codex', name: 'Codex', state: 'ready' }];

async function serve(page: Page, sources: readonly Record<string, unknown>[], shell = false) {
	await page.route('**/api/auth/status', (route) => route.fulfill({ json: { has_users: false } }));
	await page.route('**/api/setup/status', (route) => route.fulfill({ json: { apple_silicon: true, macos_supported: true, local_eligible: true, work_folder: '~/CORSIA' } }));
	await page.route('**/api/setup/progress', (route) => route.fulfill({ json: { setup_completed: false, step: 'models', local_ai: false } }));
	await page.route('**/api/setup/local-model', (route) => route.fulfill({ json: { state: 'picked', model: { name: 'Qwen 3.8', repository: 'example/qwen', download_size_bytes: 30_000_000_000, download_size_gb: 30, context_window: 65536 }, message: null } }));
	await page.route('**/api/setup/local-download', (route) => route.fulfill({ json: { ok: true, in_progress: true } }));
	await page.route('**/api/setup/first-account', (route) => route.fulfill({ json: { token: TEST_SESSION_TOKEN, user: { id: 'e2e', name: 'First Person' } } }));
	await page.route('**/api/providers/sources/recheck**', (route) => route.fulfill({ json: { sources } }));
	await page.route('**/api/providers/sources**', (route) => route.fulfill({ json: { sources } }));
	if (shell) await page.addInitScript(() => {
		const w = window as unknown as Record<string, unknown>;
		w.__CORSIA_SHELL__ = { mode: 'local' };
		w.__TAURI_INTERNALS__ = { invoke: async (command: string) => command === 'remote_discovered' ? { names: ['Studio Mac', 'Office iMac'], denied: false } : null };
	});
}

/** Whether any text or control on the screen overlaps the logo, and where the logo is; null when it is not drawn. */
async function logoReport(page: Page) {
	return page.locator('.pane-scene').last().evaluate((scene) => {
		const mark = scene.querySelector<HTMLElement>('.brand > .mark');
		if (!mark || getComputedStyle(mark).display === 'none') return null;
		const m = mark.getBoundingClientRect(), brand = scene.querySelector('.brand')!.getBoundingClientRect();
		const touching: string[] = [];
		for (const node of scene.querySelectorAll<HTMLElement>('*')) {
			if (node === mark || node.contains(mark) || mark.contains(node)) continue;
			const own = Array.from(node.childNodes).some((child) => child.nodeType === Node.TEXT_NODE && child.textContent?.trim());
			if (!own && !node.matches('input, button, img, svg')) continue;
			const r = node.getBoundingClientRect();
			if (!r.width || !r.height || getComputedStyle(node).visibility === 'hidden') continue;
			if (r.left < m.right && r.right > m.left && r.top < m.bottom && r.bottom > m.top) touching.push((node.textContent ?? node.tagName).trim().slice(0, 40));
		}
		return { touching, markWidth: m.width, markHeight: m.height, insideBrand: m.left >= brand.left - 1 && m.right <= brand.right + 1 && m.top >= brand.top - 1 && m.bottom <= brand.bottom + 1, innerWidth: window.innerWidth };
	});
}

async function checkScreen(page: Page, width: number, name: string) {
	// Measure the settled screen: the one before it has left and its slide-in has ended.
	await expect(page.locator('.pane-scene')).toHaveCount(1);
	await page.evaluate(() => Promise.all(document.getAnimations().filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => undefined))));
	const brand = page.locator('.pane-scene').last().locator('.brand');
	if (width < 1280) {
		await expect(brand, `${name}: no brand column below 1280`).toBeHidden();
	} else {
		const report = await logoReport(page);
		if (report) {
			expect(report.touching, `${name}: nothing touches the logo`).toEqual([]);
			expect(report.insideBrand, `${name}: the logo stays in the brand column`).toBe(true);
			expect(report.markHeight, `${name}: the logo is shown only at its minimum size or more`).toBeGreaterThanOrEqual(120);
		} else {
			// Only Welcome, in the shortest windows, may go without the logo.
			expect(name, `${name}: the logo may hide on Welcome alone`).toMatch(/^account/);
			expect(await page.locator('.brand .welcome').count(), `${name}: the logo hides only where Welcome fills the column`).toBe(1);
		}
	}
	const body = page.locator('.pane-scene').last().locator('.body');
	const overflow = await body.evaluate((node) => {
		const box = node.getBoundingClientRect();
		return Array.from(node.querySelectorAll<HTMLElement>('*')).filter((child) => child.getBoundingClientRect().right > box.right + 1).map((child) => `${child.tagName}.${child.className} ${Math.round(child.getBoundingClientRect().right - box.right)}px`).slice(0, 5);
	});
	expect(overflow.filter((item) => !item.startsWith('DIV.scroll ')), `${name}: the content fits its column`).toEqual([]);
	expect(await body.evaluate((node) => node.offsetHeight - node.clientHeight <= 0), `${name}: the column shows no sideways scrollbar`).toBe(true);
	if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}-${width}x${page.viewportSize()!.height}.png`, animations: 'disabled', scale: 'css' });
}

async function createAccount(page: Page, width: number) {
	if (width < 1280) await page.getByRole('button', { name: 'Continue', exact: true }).click();
	await page.locator('input[autocomplete="name"]').fill('First Person');
	await page.locator('input[autocomplete="username"]').fill('first');
	await page.locator('input[autocomplete="new-password"]').nth(0).fill('correct-password');
	await page.locator('input[autocomplete="new-password"]').nth(1).fill('correct-password');
}

test.describe('@surface:first-run setup layout', () => {
	test.beforeEach(({}, testInfo) => { test.skip(testInfo.project.name !== 'chromium-desktop', 'The sizes are set by this spec; one project proves them.'); });
	for (const [width, height] of SIZES) {
		test(`every screen at ${width}x${height} keeps text clear of the logo`, async ({ page }) => {
			await page.setViewportSize({ width, height });
			await serve(page, notInstalled);
			await page.goto('/');
			await expect(page.getByRole('heading', { name: 'Welcome to CORSIA' })).toBeVisible();
			if (width < 1280) await checkScreen(page, width, 'welcome');
			await createAccount(page, width);
			await expect(page.getByText('Your projects and files live here.')).toBeVisible();
			await checkScreen(page, width, 'account');
			await page.getByRole('button', { name: 'Continue', exact: true }).click();
			await expect(page.getByRole('heading', { name: 'Your subscriptions' })).toBeVisible();
			await checkScreen(page, width, 'subscriptions');
			await page.getByRole('button', { name: 'Neither', exact: true }).click();
			await expect(page.getByRole('heading', { name: 'Providers & models' })).toBeVisible();
			await checkScreen(page, width, 'models');
			// A row's toggle and button sit beside its text: the row never grows past the content column's cap (10 x 48px).
			const rows = await page.locator('section.item').evaluateAll((items) => items.map((item) => {
				const row = item.getBoundingClientRect(), actions = Array.from(item.querySelectorAll('button, [role="switch"]')).map((node) => node.getBoundingClientRect());
				return { width: row.width, farthest: Math.max(0, ...actions.map((a) => a.right - row.left)), controls: actions.length };
			}));
			expect(rows.length, 'the provider rows are on screen').toBeGreaterThan(0);
			for (const row of rows) { expect(row.width).toBeLessThanOrEqual(480 + 1); expect(row.farthest).toBeLessThanOrEqual(480 + 1); }
			expect(rows.some((row) => row.controls > 0), 'at least one row has a control to measure').toBe(true);
		});

		test(`the connect screens at ${width}x${height} keep text clear of the logo`, async ({ page }) => {
			await page.setViewportSize({ width, height });
			await serve(page, ready, true);
			await page.goto('/');
			await expect(page.getByRole('heading', { name: 'Welcome to CORSIA' })).toBeVisible();
			await page.getByRole('button', { name: 'Connect to CORSIA on another computer' }).click();
			await expect(page.locator('[data-setup-page="connect-choose"]').last()).toBeVisible();
			await expect(page.getByText('Studio Mac')).toBeVisible();
			await checkScreen(page, width, 'connect-choose');
			await page.getByText('Studio Mac').click();
			await page.getByRole('button', { name: 'Continue', exact: true }).click();
			await expect(page.locator('[data-setup-page="connect-code"]').last()).toBeVisible();
			await checkScreen(page, width, 'connect-code');
		});
	}
});
