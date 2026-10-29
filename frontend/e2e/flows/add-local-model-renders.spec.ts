import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection } from '../support/corsia';

const SHOTS = process.env.ADD_LOCAL_MODEL_SHOTS_DIR;
const server = {
	id: 'omlx-local', provider: 'omlx', instance_id: 'this-mac', machine_id: 'this-mac',
	instance_name: 'This Mac', kind: 'managed', port: 11434, status: 'online', source: 'manual',
	latency_ms: 1, memory_total_mb: 65536, memory_used_mb: 0, gpu_info: null,
	endpoints: [{ id: 'omlx-local-endpoint', server_id: 'omlx-local', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: 1 }],
	active_host: '127.0.0.1', api_base: null, mgmt_base: null, corsia_omlx: true,
};
const provider = {
	name: 'omlx', label: 'oMLX', type: 'network', has_key: false, masked_key: null,
	api_base: null, docs_url: null, models: [], enabled: true,
};
const recommendations = {
	state: 'ready',
	memory_gb: 64,
	disk_free_gb: 20,
	downloads: [],
	recommended_repository: 'google/gemma-4-31b-it',
	memory_room_gb: 40,
	models: [
		{ name: 'Gemma 4 31B IT', repository: 'google/gemma-4-31b-it', download_size_bytes: 20 * 1000 ** 3, download_size_gb: 20, context_window: 262144, quantization_bits: 4, relative_speed: 0.85, smarts_level: 4, tokens_per_second: 24, resident_bytes: 22 * 1024 ** 3, fits_now: true, disk: { state: 'ready' } },
		{ name: 'Qwen3.8-Flash-Next', repository: 'qwen/qwen3.8-flash-next', download_size_bytes: 16 * 1000 ** 3, download_size_gb: 16, context_window: 262144, quantization_bits: 4, relative_speed: 1.0, smarts_level: 3, tokens_per_second: 60, resident_bytes: 18 * 1024 ** 3, fits_now: true, disk: { state: 'ready' } },
		{ name: 'Qwen3.8-14B', repository: 'qwen/qwen3.8-14b', download_size_bytes: 18 * 1000 ** 3, download_size_gb: 18, context_window: 262144, quantization_bits: 4, relative_speed: 1.44, smarts_level: 2, tokens_per_second: 90, resident_bytes: 12 * 1024 ** 3, fits_now: true, disk: { state: 'ready' } },
		{ name: 'Qwen3.8-27B', repository: 'qwen/qwen3.8-27b', download_size_bytes: 31 * 1000 ** 3, download_size_gb: 31, context_window: 262144, smarts_level: 4.5, tokens_per_second: 18, resident_bytes: 48 * 1024 ** 3, fits_now: false, disk: { state: 'insufficient', required_bytes: 31 * 1000 ** 3 } },
	],
};type RenderState = 'recommendations' | 'downloading' | 'complete' | '24gb';

function sourceFor(state: RenderState) {
	return {
		source: 'omlx', name: 'Local model', state: 'ready',
		eligible: state === '24gb' ? false : true,
		corsia_omlx_online: true,
		settings_download: state === 'downloading'
			? { repository: 'qwen/qwen3.8-27b', name: 'Qwen3.8-27B', percent: 40, failed: false }
			: null,
	};
}

function serverRow(id: string, display_name: string, size_bytes: number) {
	// Exact /api/settings/network-servers/{id}/models UI shape: no display-only fields.
	return { id, model_id: id, repo_id: id, display_name, context_length: 262144, size_bytes };
}

function modelRows(state: RenderState) {
	if (state === '24gb') return { loaded: [], available: [] };
	if (state === 'downloading') return {
		loaded: [serverRow('qwen3.8-flash-next', 'Qwen3.8-Flash-Next', 16 * 1024 ** 3)],
		available: [serverRow('gemma-4-31b-it', 'Gemma 4 31B IT', 20 * 1024 ** 3)],
	};
	return {
		loaded: [serverRow('qwen3.8-27b', 'Qwen3.8-27B', 31 * 1024 ** 3)],
		available: [serverRow('qwen3.8-flash-next', 'Qwen3.8-Flash-Next', 16 * 1024 ** 3)],
	};
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
			device_name: 'This Mac', total_gb: state === '24gb' ? 24 : 64,
			free_gb: state === '24gb' ? 10 : 34, models_used_gb: 0, other_used_gb: 0, safe_free_gb: 8,
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
	const downloads = state === 'complete' ? [{
		name: 'Qwen3.8-Flash-Next', repository: 'qwen/qwen3.8-flash-next', model: 'qwen3.8-flash-next',
		download_size_bytes: 16 * 1000 ** 3, download_size_gb: 16, context_window: 262144,
		status: 'available', speed: 52, detail: null, server_id: 'omlx-local',
	}] : recommendations.downloads;
	await page.route('**/api/providers/local/recommendations', async (route) => {
		await route.fulfill({ json: { ...recommendations, downloads } });
	});
	await page.route('**/api/providers/local/downloads', async (route) => {
		if (route.request().method() === 'POST') {
			expect(route.request().postDataJSON()).toEqual({ repository: 'qwen/qwen3.8-flash-next' });
			await route.fulfill({ json: { ok: true } });
			return;
		}
		// Settings rows read the durable records from this route, never the recommender.
		await route.fulfill({ json: downloads });
	});
	// The app may request these unrelated catalog projections while Settings mounts.
	await page.route('**/api/vendor-accounts**', async (route) => route.fulfill({ json: { accounts: [] } }));
	await page.route('**/api/models**', async (route) => route.fulfill({ json: { models: [], active_local_threads: 0 } }));
}

const studio = {
	...server,
	id: 'studio', instance_id: 'studio', machine_id: 'studio', instance_name: 'Studio', source: 'discovered',
	active_host: 'Studio.local', last_seen_at: new Date().toISOString(), corsia_omlx: false,
	endpoints: [{ id: 'studio-endpoint', server_id: 'studio', host: 'Studio.local', kind: 'remote', last_ok_at: null, last_latency_ms: 3 }],
};
const studioPickerModel = {
	id: 'omlx@studio/studio-model', selector: 'omlx@studio/studio-model', litellm_model: 'omlx@studio/studio-model',
	name: 'Studio model', display_name: 'Studio model', connection: 'omlx', provider: 'omlx', backend: 'network',
	server_name: 'Studio', status: 'ready', available: true, desc: '', size: '', speed: '', capabilities: {},
};
const localPickerModel = {
	...studioPickerModel,
	id: 'omlx@omlx-local/local-model', selector: 'omlx@omlx-local/local-model', litellm_model: 'omlx@omlx-local/local-model',
	name: 'Local model', display_name: 'Local model', server_name: 'CORSIA',
};
const studioRows = {
	loaded: [serverRow('studio-qwen', 'Studio Qwen', 16 * 1024 ** 3)],
	available: [serverRow('studio-gemma', 'Studio Gemma', 20 * 1024 ** 3)],
};

async function fixtureOwnershipState(
	page: Page,
	servers: typeof server[],
	source: Record<string, unknown>,
	catalog: Record<string, unknown>[],
	recommendationFailure = false,
	modelsByServer: Record<string, { loaded: object[]; available: object[] }> = {},
) {
	await page.route('**/api/settings/providers?**', async (route) => route.fulfill({ json: { providers: [provider] } }));
	await page.route('**/api/settings/network-servers?**', async (route) => route.fulfill({ json: { servers } }));
	await page.route('**/api/settings/network-servers/*/models?**', async (route) => {
		const serverForRequest = servers.find((candidate) => route.request().url().includes(`/network-servers/${candidate.id}/models`));
		await route.fulfill({ json: modelsByServer[serverForRequest?.id ?? ''] ?? { loaded: [], available: [] } });
	});
	await page.route('**/api/settings/network-servers/*/jobs/active?**', async (route) => route.fulfill({ json: { jobs: [] } }));
	await page.route('**/api/system/host-resources?**', async (route) => route.fulfill({ json: {
		device_name: 'This Mac', total_gb: 64, free_gb: 34, models_used_gb: 0, other_used_gb: 0, safe_free_gb: 8, safe_reserve_gb: 8, by_provider: {},
	} }));
	await page.route('**/api/providers/sources', async (route) => route.fulfill({ json: { sources: [source] } }));
	await page.route('**/api/providers/local/recommendations**', async (route) => route.fulfill(
		recommendationFailure
			? { status: 503, json: { detail: "Couldn’t check which models fit this computer." } }
			: { json: recommendations },
	));
	await page.route('**/api/providers/local/downloads**', async (route) => route.fulfill({ json: [] }));
	await page.route('**/api/models**', async (route) => route.request().method() === 'GET'
		? route.fulfill({ json: { models: catalog, active_local_threads: 0 } }) : route.continue());
	await page.route('**/api/vendor-accounts**', async (route) => route.fulfill({ json: { accounts: [] } }));
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
	if (!SHOTS) return;
	await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true, animations: 'disabled', scale: 'css' });
}

test('renders the approved local-model evidence states from intercepted fixtures', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'This evidence test sets its required desktop and phone viewports itself.');
	test.setTimeout(180_000);
	for (const { viewport, theme } of [
		{ viewport: { name: '1440', width: 1440, height: 900 }, theme: 'light' as const },
		{ viewport: { name: '1440', width: 1440, height: 900 }, theme: 'dark' as const },
		{ viewport: { name: '393', width: 393, height: 852 }, theme: 'light' as const },
		{ viewport: { name: '393', width: 393, height: 852 }, theme: 'dark' as const },
	]) {
			for (const state of ['recommendations', 'downloading', 'complete', '24gb'] as const) {
				await page.unrouteAll({ behavior: 'ignoreErrors' });
				await page.setViewportSize({ width: viewport.width, height: viewport.height });
				await page.emulateMedia({ colorScheme: theme });
				await fixtureLocalModelState(page, state);
				const card = await openLocalProvider(page);
				if (state === 'recommendations') {
					await card.getByRole('button', { name: 'Add model', exact: true }).click();
					const sheet = page.locator('.modal').filter({ hasText: 'Add model' });
					await expect(sheet).toContainText('Picked for this computer · 64 GB memory · 20 GB disk free');
					await expect(sheet).toContainText('Recommended');
					await expect(sheet).toContainText('20 GB · ~24 tokens/s');
					await expect(sheet).toContainText('16 GB · ~60 tokens/s');
					await expect(sheet).toContainText('Quality 4 of 5 · 256K context');
					await expect(sheet).not.toContainText('Qwen3.8-27B');
					await expect(sheet.getByRole('button', { name: /Download/ })).toHaveCount(3);
				} else if (state === 'downloading') {
					const downloadingRow = card.locator('.mr').filter({ hasText: 'Qwen3.8-27B' });
					await expect(card).toContainText('Downloading 40%');
					await expect(downloadingRow).toHaveCount(1);
					// R6: the standard row; its switch is on the row, disabled until the model is on disk.
					await expect(downloadingRow.locator('.tg')).toBeDisabled();
					// Download progress is text and a spinner, with no progress hairline.
					expect(await downloadingRow.evaluate((row) => getComputedStyle(row, '::after').content)).toBe('none');
					await expect(card.getByRole('button', { name: 'Cancel' })).toBeVisible();
				} else if (state === 'complete') {
					await expect(card).toContainText('262k context · 16 GB · 52 tokens/s');
					await expect(card).toContainText('Settings measured on Macs like this one · omlx.ai');
					await expect(card.getByRole('button', { name: 'Add model', exact: true })).toBeVisible();
				} else {
					await expect(page.getByText(/24 GB memory/)).toBeVisible();
					await expect(card).toContainText('Adding a local model through CORSIA needs at least 32 GB of memory. Smaller models and future updates may bring local models to computers with less memory. Connect a subscription or an API provider.');
					await expect(card).not.toContainText('No models yet.');
					await expect(card.getByRole('button', { name: 'Add model', exact: true })).toHaveCount(0);
					await expect(card.getByRole('button', { name: 'Add provider', exact: true })).toBeVisible();
				}
				await capture(page, `${state}-${viewport.name}-${theme}`);
			}
	}
});

test('renders ownership, picker, recommender-failure, and picker-download evidence states', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'This evidence test sets its required desktop and phone viewports itself.');
	test.setTimeout(120_000);
	for (const { viewport, theme } of [
		{ viewport: { name: '1440', width: 1440, height: 900 }, theme: 'light' as const },
		{ viewport: { name: '1440', width: 1440, height: 900 }, theme: 'dark' as const },
		{ viewport: { name: '393', width: 393, height: 852 }, theme: 'light' as const },
	]) {
		await page.setViewportSize({ width: viewport.width, height: viewport.height });
		await page.emulateMedia({ colorScheme: theme });

		// A: Studio is a person's reachable oMLX. Its models remain visible, but CORSIA cannot administer it.
		await page.unrouteAll({ behavior: 'ignoreErrors' });
		await fixtureOwnershipState(
			page, [studio], { source: 'omlx', name: 'Local model', state: 'ready', eligible: true, corsia_omlx_online: false },
			[studioPickerModel], false, { studio: studioRows },
		);
		await loginByUrl(page);
		await openSettings(page);
		await openSettingsSection(page, 'Providers');
		const studioCard = page.locator('[data-server-card="studio"]');
		const localSetup = page.locator('[data-provider-card="local-setup"]');
		await studioCard.locator('.pr-expand').click();
		await expect(page.getByText('Studio', { exact: true })).toBeVisible();
		await expect(studioCard).toContainText('Studio Qwen');
		await expect(studioCard).toContainText('Studio Gemma');
		await expect(studioCard.getByRole('button', { name: 'Add model', exact: true })).toHaveCount(0);
		await expect(localSetup).toContainText('Local model');
		await expect(localSetup).toContainText('This computer');
		await expect(localSetup.getByRole('button', { name: 'Set up', exact: true })).toBeVisible();
		await capture(page, `a-studio-models-${viewport.name}-${theme}`);

		// A-empty: the same person-owned card says where models are managed, beside the Settings setup route.
		await page.unrouteAll({ behavior: 'ignoreErrors' });
		await fixtureOwnershipState(page, [studio], { source: 'omlx', name: 'Local model', state: 'ready', eligible: true, corsia_omlx_online: false }, [studioPickerModel]);
		await loginByUrl(page);
		await openSettings(page);
		await openSettingsSection(page, 'Providers');
		const emptyStudioCard = page.locator('[data-server-card="studio"]');
		await emptyStudioCard.locator('.pr-expand').click();
		await expect(emptyStudioCard).toContainText('No models on Studio. Add them in oMLX on that computer.');
		await expect(page.locator('[data-provider-card="local-setup"]').getByRole('button', { name: 'Set up', exact: true })).toBeVisible();
		await capture(page, `a-empty-studio-${viewport.name}-${theme}`);

		// B: the picker preserves Studio and offers the existing local setup route, not an admin action.
		await page.unrouteAll({ behavior: 'ignoreErrors' });
		await fixtureOwnershipState(page, [studio], { source: 'omlx', name: 'Local model', state: 'ready', eligible: true, corsia_omlx_online: false }, [studioPickerModel]);
		await loginByUrl(page);
		await page.locator('button.model-chip').click();
		const picker = page.locator('.model-sheet');
		await expect(picker.locator('section[aria-label="Studio"]')).toBeVisible();
		await expect(picker.locator('section[aria-label="CORSIA"]')).toContainText('Set up');
		await expect(picker).not.toContainText('Add a local model');
		await capture(page, `b-picker-setup-${viewport.name}-${theme}`);

		// C and E: a CORSIA-keyed loopback is the only card allowed to add a model.
		await page.unrouteAll({ behavior: 'ignoreErrors' });
		await fixtureOwnershipState(page, [studio, server], { source: 'omlx', name: 'Local model', state: 'ready', eligible: true, corsia_omlx_online: true }, [studioPickerModel]);
		await loginByUrl(page);
		await openSettings(page);
		await openSettingsSection(page, 'Providers');
		const corsiaCard = page.locator('[data-server-card="omlx-local"]');
		const studioCardInC = page.locator('[data-server-card="studio"]');
		await corsiaCard.locator('.pr-expand').click();
		await studioCardInC.locator('.pr-expand').click();
		await expect(corsiaCard.getByRole('button', { name: 'Add model', exact: true })).toBeVisible();
		const studioEmpty = studioCardInC.locator('.phase-stub');
		await expect(studioEmpty).toContainText('No models on Studio. Add them in oMLX on that computer.');
		expect(await studioEmpty.evaluate((element) => getComputedStyle(element).paddingLeft)).not.toBe('0px');
		await expect(page.locator('[data-server-card="studio"]').getByRole('button', { name: 'Add model', exact: true })).toHaveCount(0);
		await capture(page, `c-corsia-and-studio-${viewport.name}-${theme}`);

		await page.unrouteAll({ behavior: 'ignoreErrors' });
		await fixtureOwnershipState(page, [server], { source: 'omlx', name: 'Local model', state: 'ready', eligible: true, corsia_omlx_online: true }, [], true);
		await loginByUrl(page);
		await openSettings(page);
		await openSettingsSection(page, 'Providers');
		const failureCard = page.locator('[data-server-card="omlx-local"]');
		await failureCard.locator('.pr-expand').click();
		await failureCard.getByRole('button', { name: 'Add model', exact: true }).click();
		const failureSheet = page.locator('.modal').filter({ hasText: 'Add model' });
		await expect(failureSheet).toContainText("Couldn’t check which models fit this computer.");
		await expect(failureSheet.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
		await capture(page, `e-recommender-failed-${viewport.name}-${theme}`);

		// F picker half: an in-progress Settings download is visible beside the Local group.
		await page.unrouteAll({ behavior: 'ignoreErrors' });
		await fixtureOwnershipState(page, [server], sourceFor('downloading'), [localPickerModel]);
		await loginByUrl(page);
		await page.locator('button.model-chip').click();
		const downloadPicker = page.locator('.model-sheet');
		await expect(downloadPicker.locator('section[aria-label="CORSIA"]')).toContainText('Downloading Qwen3.8-27B · 40%');
		await capture(page, `f-picker-downloading-${viewport.name}-${theme}`);
	}
});
