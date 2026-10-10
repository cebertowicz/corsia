import { expect, test, type Locator, type Page } from '@playwright/test';
import { loginByUrl, openSettings } from './support/corsia';

const harnessRows = [
	...Array.from({ length: 5 }, (_, index) => ({ id: `claude-${index}`, harness: 'claude-code', available: true, enabled: true, display_name: `Claude ${index}` })),
	...Array.from({ length: 7 }, (_, index) => ({ id: `codex-${index}`, harness: 'codex', available: true, enabled: true, display_name: `Codex ${index}` })),
];
const accounts = [
	...Array.from({ length: 3 }, (_, index) => ({ id: `claude-account-${index}`, provider: 'claude-code', name: `Claude ${index}`, is_default: index === 0, state: 'signed_in', revision: 1, identity: {}, label: { name: `Claude ${index}`, email: null, text: `Claude ${index}`, custom: null } })),
	...Array.from({ length: 2 }, (_, index) => ({ id: `codex-account-${index}`, provider: 'codex', name: `Codex ${index}`, is_default: index === 0, state: 'signed_in', revision: 1, identity: {}, label: { name: `Codex ${index}`, email: null, text: `Codex ${index}`, custom: null } })),
];
const providers = [
	{
		name: 'external',
		label: 'External',
		type: 'cloud',
		has_key: true,
		masked_key: null,
		api_base: null,
		docs_url: null,
		models: ['external-test-model'],
		enabled: true,
	},
	{
		name: 'omlx',
		label: 'oMLX',
		type: 'network',
		has_key: false,
		masked_key: null,
		api_base: null,
		docs_url: null,
		models: ['omlx-test-model'],
		enabled: true,
	},
];
const networkServers = [{
	id: 'omlx-test',
	provider: 'omlx',
	instance_id: 'omlx-test',
	instance_name: 'oMLX',
	kind: 'managed',
	port: 8080,
	status: 'online',
	source: 'manual',
	latency_ms: 1,
	memory_total_mb: 16384,
	memory_used_mb: 4096,
	gpu_info: 'test',
	endpoints: [{ id: 'omlx-test-local', server_id: 'omlx-test', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: 1 }],
	active_host: '127.0.0.1',
	api_base: 'http://127.0.0.1:8080',
	mgmt_base: null,
}];

type PointName = 'centre' | 'left edge' | 'right edge';
type RenderProvider = 'claude-code' | 'external';

const points: { name: PointName; at: (width: number, height: number) => { x: number; y: number } }[] = [
	{ name: 'centre', at: (width, height) => ({ x: width / 2, y: height / 2 }) },
	{ name: 'left edge', at: (_, height) => ({ x: 1, y: height / 2 }) },
	{ name: 'right edge', at: (width, height) => ({ x: width - 1, y: height / 2 }) },
];

async function mockProviders(page: Page) {
	await page.route('**/api/settings/models**', (route) => route.fulfill({ json: { models: harnessRows } }));
	await page.route('**/api/vendor-accounts**', (route) => route.fulfill({ json: { accounts } }));
	await page.route('**/api/settings/providers**', (route) => route.fulfill({ json: { providers } }));
	await page.route('**/api/settings/network-servers**', (route) => route.fulfill({ json: { servers: networkServers } }));
}

async function openProviders(page: Page) {
	await loginByUrl(page, { waitForComposer: false });
	await openSettings(page);
	await page.getByRole('button', { name: 'Providers' }).click();
}

function expandControl(card: Locator): Locator {
	return card.locator(':scope > .pr-head > .pr-expand');
}

async function settingsGear(page: Page, providerName: RenderProvider): Promise<Locator> {
	const gear = page.locator(`[data-provider-card="${providerName}"]`).getByRole('button', { name: 'Settings' });
	await gear.scrollIntoViewIfNeeded();
	return gear;
}

for (const viewport of [
	{ name: '1280x800', width: 1280, height: 800 },
	{ name: '1440x900', width: 1440, height: 900 },
]) {
	for (const point of points) {
		test(`Claude Code ${point.name} Settings click opens its dialog at ${viewport.name}`, async ({ page }) => {
			await page.setViewportSize({ width: viewport.width, height: viewport.height });
			await mockProviders(page);
			await openProviders(page);
			const gear = await settingsGear(page, 'claude-code');
			const box = await gear.boundingBox();
			expect(box).not.toBeNull();
			const offset = point.at(box!.width, box!.height);
			await page.mouse.click(box!.x + offset.x, box!.y + offset.y);
			const dialog = page.getByRole('dialog');
			await expect(dialog).toBeVisible();
		});
	}
}

for (const viewport of [
	{ name: '1280x800', width: 1280, height: 800 },
	{ name: '1440x900', width: 1440, height: 900 },
	{ name: '390x844', width: 390, height: 844 },
]) {
	test(`harness and External Settings buttons meet the 24px target at ${viewport.name}`, async ({ page }) => {
		await page.setViewportSize({ width: viewport.width, height: viewport.height });
		await mockProviders(page);
		await openProviders(page);

		for (const providerName of ['claude-code', 'external'] as const) {
			const gear = await settingsGear(page, providerName);
			const { width, height } = await gear.evaluate((element) => {
				const rect = element.getBoundingClientRect();
				return { width: rect.width, height: rect.height };
			});
			expect(width, `${providerName} Settings width`).toBeGreaterThanOrEqual(24);
			expect(height, `${providerName} Settings height`).toBeGreaterThanOrEqual(24);
		}
	});
}

for (const providerName of ['external', 'omlx']) {
	test(`${providerName} settings opens without changing its card expansion`, async ({ page }) => {
		await page.setViewportSize({ width: 1280, height: 800 });
		await mockProviders(page);
		await openProviders(page);
		const card = page.locator(`[data-provider-card="${providerName}"]`);
		const gear = card.getByRole('button', { name: 'Settings' });
		await gear.scrollIntoViewIfNeeded();
		const before = await expandControl(card).getAttribute('aria-expanded');
		await gear.click();
		const dialog = page.getByRole('dialog');
		await expect(dialog).toBeVisible();
		await page.getByRole('button', { name: 'Close', exact: true }).click();
		expect(await expandControl(card).getAttribute('aria-expanded')).toBe(before);
	});
}

test('External provider controls are sibling native buttons in keyboard order', async ({ page }) => {
	await page.setViewportSize({ width: 1280, height: 800 });
	await mockProviders(page);
	await openProviders(page);
	const card = page.locator('[data-provider-card="external"]');
	const expand = expandControl(card);
	const settings = card.getByRole('button', { name: 'Settings' });
	const enable = card.getByRole('button', { name: 'Disable' });

	await expect(card.locator(':scope > .pr-head > button')).toHaveCount(3);
	await expect(expand).toHaveAttribute('aria-expanded', 'false');
	await expand.focus();
	await page.keyboard.press('Tab');
	await expect(settings).toBeFocused();
	await page.keyboard.press('Tab');
	await expect(enable).toBeFocused();
});

for (const providerName of ['claude-code', 'external'] as const) {
	for (const theme of ['dark', 'light'] as const) {
		for (const viewport of [
			{ name: '1280x800', width: 1280, height: 800 },
			{ name: '390x844', width: 390, height: 844 },
		]) {
			test(`opened ${providerName} settings renders in ${theme} at ${viewport.name}`, async ({ page }, testInfo) => {
				await page.emulateMedia({ colorScheme: theme });
				await page.setViewportSize({ width: viewport.width, height: viewport.height });
				await mockProviders(page);
				await openProviders(page);
				const gear = await settingsGear(page, providerName);
				await gear.click();
				await expect(page.getByRole('dialog')).toBeVisible();
				await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
				await page.screenshot({ path: testInfo.outputPath(`providers-${providerName}-opened-${theme}-${viewport.name}.png`) });
			});
		}
	}
}
