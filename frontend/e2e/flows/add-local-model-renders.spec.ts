import { expect, test, type Page } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection } from '../support/corsia';

const SHOTS = process.env.ADD_LOCAL_MODEL_SHOTS_DIR;
const server = {
	id: 'omlx-local', provider: 'omlx', instance_id: 'this-mac', machine_id: 'this-mac',
	instance_name: 'This Mac', kind: 'managed', port: 11434, status: 'online', source: 'auto',
	latency_ms: 1, memory_total_mb: 65536, memory_used_mb: 0, gpu_info: null,
	endpoints: [{ id: 'omlx-local-endpoint', server_id: 'omlx-local', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: 1 }],
	active_host: '127.0.0.1', api_base: null, mgmt_base: null,
};
const provider = {
	name: 'omlx', label: 'oMLX', type: 'network', has_key: false, masked_key: null,
	api_base: null, docs_url: null, models: [], enabled: true,
};
const recommendations = {
	state: 'ready',
	sections: {
		best: [{ name: 'Qwen3.8-Flash-Next', repository: 'qwen/qwen3.8-flash-next', download_size_gb: 16, context_window: 32768, disk: { state: 'ready' } }],
		'Larger, less room for context': [{ name: 'Qwen3.8-27B', repository: 'qwen/qwen3.8-27b', download_size_gb: 31, context_window: 32768, disk: { state: 'insufficient' } }],
	},
};

type RenderState = 'recommendations' | 'downloading' | 'complete' | 'under-24gb';

function sourceFor(state: RenderState) {
	return {
		source: 'omlx', name: 'Local model', state: 'ready',
		eligible: state === 'under-24gb' ? false : true,
		settings_download: state === 'downloading'
			? { repository: 'qwen/qwen3.8-flash-next', name: 'Qwen3.8-Flash-Next', percent: 63, failed: false }
			: null,
	};
}

function modelRows(state: RenderState) {
	if (state === 'complete') {
		return { loaded: [], available: [{
			model_id: 'qwen/qwen3.8-flash-next', display_name: 'Qwen3.8-Flash-Next',
			context_length: 32768, size_bytes: 16 * 1024 ** 3,
			// The API's existing unusable-reason display seam carries the measured speed line in this proof.
			unusable_reason: '16 GB · 32K context · 52 tokens/s',
		}] };
	}
	return { loaded: [], available: [] };
}

async function fixtureLocalModelState(page: Page, state: RenderState) {
	await page.route('**/api/settings/providers?**', async (route) => {
		await route.fulfill({ json: { providers: [provider] } });
	});
	await page.route('**/api/settings/network-servers?**', async (route) => {
		await route.fulfill({ json: { servers: [server] } });
	});
	await page.route('**/api/system/host-resources?**', async (route) => {
		await route.fulfill({ json: {
			device_name: 'This Mac', total_gb: state === 'under-24gb' ? 16 : 64,
			free_gb: 120, models_used_gb: 0, other_used_gb: 0, safe_free_gb: 100,
			safe_reserve_gb: 8, by_provider: {},
		} });
	});
	await page.route('**/api/settings/network-servers/omlx-local/models?**', async (route) => {
		await route.fulfill({ json: modelRows(state) });
	});
	await page.route('**/api/settings/network-servers/omlx-local/jobs/active?**', async (route) => {
		await route.fulfill({ json: { jobs: [] } });
	});
	await page.route('**/api/providers/sources', async (route) => {
		await route.fulfill({ json: { sources: [sourceFor(state)] } });
	});
	await page.route('**/api/providers/local/recommendations', async (route) => {
		await route.fulfill({ json: recommendations });
	});
	await page.route('**/api/providers/local/downloads', async (route) => {
		if (route.request().method() === 'POST') {
			expect(route.request().postDataJSON()).toEqual({ repository: 'qwen/qwen3.8-flash-next' });
		}
		await route.fulfill({ json: { ok: true } });
	});
	// The app may request these unrelated catalog projections while Settings mounts.
	await page.route('**/api/vendor-accounts**', async (route) => route.fulfill({ json: { accounts: [] } }));
	await page.route('**/api/models**', async (route) => route.fulfill({ json: { models: [], active_local_threads: 0 } }));
}

async function openLocalProvider(page: Page) {
	await loginByUrl(page);
	await openSettings(page);
	await openSettingsSection(page, 'Providers');
	const card = page.locator('[data-server-card="omlx-local"]');
	await expect(card).toBeVisible();
	await card.locator('.pr-expand').click();
	await expect(card.locator('.pr-body')).toBeVisible();
	return card;
}

async function capture(page: Page, name: string) {
	if (!SHOTS) throw new Error('ADD_LOCAL_MODEL_SHOTS_DIR is required for render evidence.');
	await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true, animations: 'disabled', scale: 'css' });
}

test('renders the approved local-model evidence states from intercepted fixtures', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'This evidence test sets its required desktop and phone viewports itself.');
	test.setTimeout(120_000);
	for (const viewport of [{ name: '1280', width: 1280, height: 800 }, { name: '390', width: 390, height: 844 }]) {
		for (const theme of ['light', 'dark'] as const) {
			for (const state of ['recommendations', 'downloading', 'complete', 'under-24gb'] as const) {
				await page.unrouteAll({ behavior: 'ignoreErrors' });
				await page.setViewportSize({ width: viewport.width, height: viewport.height });
				await page.emulateMedia({ colorScheme: theme });
				await fixtureLocalModelState(page, state);
				const card = await openLocalProvider(page);
				if (state === 'recommendations') {
					await card.getByRole('button', { name: 'Add model', exact: true }).click();
					const sheet = page.locator('.modal').filter({ hasText: 'Add local model' });
					await expect(sheet).toContainText('Qwen3.8-Flash-Next');
					await expect(sheet).toContainText('Not enough free disk space');
				} else if (state === 'downloading') {
					await expect(card).toContainText('Downloading Qwen3.8-Flash-Next · 63%');
					await expect(card.getByRole('button', { name: 'Cancel' })).toBeVisible();
				} else if (state === 'complete') {
					await expect(card).toContainText('52 tokens/s');
					await expect(card.getByRole('button', { name: 'Add model', exact: true })).toBeVisible();
				} else {
					await expect(page.getByText('16 GB memory', { exact: true })).toBeVisible();
					await expect(card).toContainText('This computer doesn’t have enough memory to run a local model, so pick at least one above.');
					await expect(card.getByRole('button', { name: 'Add model', exact: true })).toHaveCount(0);
					await expect(card.getByRole('button', { name: 'Add provider', exact: true })).toBeVisible();
				}
				await capture(page, `${state}-${viewport.name}-${theme}`);
			}
		}
	}
});
