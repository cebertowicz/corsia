import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AddLocalModelSheet from './AddLocalModelSheet.svelte';
import { token } from '$lib/stores/auth';

let component: ReturnType<typeof mount> | null = null;
let host: HTMLElement;

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host?.remove();
	vi.unstubAllGlobals();
	token.set(null);
});

async function render(body: object) {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('test-token');
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => body })));
	component = mount(AddLocalModelSheet, { target: host, props: { onClose: vi.fn() } });
	await vi.waitFor(() => expect(host.textContent).not.toContain('Checking which models'));
	flushSync();
}

const ready = { disk: { state: 'ready' } };

describe('AddLocalModelSheet', () => {
	async function renderFailure(status: number, detail: string) {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status, json: async () => ({ detail }) })));
		component = mount(AddLocalModelSheet, { target: host, props: { onClose: vi.fn() } });
		await vi.waitFor(() => expect(document.body.textContent).toContain(detail));
		flushSync();
	}
	const tryAgain = () => [...document.body.querySelectorAll('button')].some((button) => button.textContent?.includes('Retry'));

	it('shows the backend sentence for a failed check and offers Retry, since asking again can help', async () => {
		await renderFailure(503, "Couldn’t check which models fit this computer.");
		expect(tryAgain()).toBe(true);
	});

	it('shows a cause the person must fix first without a Retry that cannot succeed', async () => {
		await renderFailure(409, "Local models aren’t set up. Select Set up to continue.");
		expect(tryAgain()).toBe(false);
	});

	it('shows the server guidance when a stale setup offer is opened on a non-macOS host', async () => {
		host = document.createElement('div'); document.body.appendChild(host); token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ detail: { code: 'never_local_host', message: "Local models don't run on this computer. Connect a provider." } }) })));
		component = mount(AddLocalModelSheet, { target: host, props: { onClose: vi.fn() } });
		await vi.waitFor(() => expect(document.body.textContent).toContain("Local models don't run on this computer. Connect a provider."));
		expect(tryAgain()).toBe(false);
	});

	it('shows the update message from oMLX without Retry', async () => {
		await render({ state: 'update_required', message: 'Update oMLX to 0.4.2 or later to add a local model.', models: [] });
		expect(document.body.textContent).toContain('Update oMLX to 0.4.2 or later to add a local model.');
		expect(tryAgain()).toBe(false);
	});

	it('uses the backend Mac-room verdict rather than a frontend memory comparison', async () => {
		const row = (name: string, fits_now: boolean) => ({ name, repository: `Jundot/${name}`, resident_bytes: 20.004 * 1024 ** 3, fits_now, ...ready });
		await render({ state: 'ready', memory_room_gb: 20, models: [row('Fits', true), row('Short', false)] });
		expect(document.body.textContent).toContain('Fits');
		expect(document.body.textContent).not.toContain('Short');
		expect(document.body.textContent).not.toContain('of memory');
	});

	it('shows the no-starter empty state when the recommender has no model', async () => {
		await render({ state: 'ready', models: [] });
		expect(document.body.textContent).toContain('No recommended models fit this computer.');
		expect(document.body.textContent).not.toContain('Download');
	});

	it('marks the recommended build and uses the same download style for each build', async () => {
		await render({ state: 'ready', recommended_repository: 'Jundot/Qwen', models: [
			{ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb: 12, context_window: 32768, fits_now: true, ...ready },
			{ name: 'Other', repository: 'Jundot/Other', download_size_gb: 16, context_window: 32768, fits_now: true, ...ready },
		] });
		expect(document.body.textContent).toContain('Recommended');
		const downloads = [...document.body.querySelectorAll<HTMLButtonElement>('button')].filter((button) => button.textContent?.includes('Download'));
		expect(downloads).toHaveLength(2);
		expect(downloads[0].classList.contains('secondary')).toBe(true);
		expect(downloads[1].classList.contains('secondary')).toBe(true);
	});

	it('is a solid sheet and sorts with the Settings radiogroup, Quality first', async () => {
		await render({ state: 'ready', recommended_repository: 'Jundot/Slow', models: [
			{ name: 'Slow', repository: 'Jundot/Slow', download_size_gb: 12, context_window: 32768, smarts_level: 5, tokens_per_second: 20, fits_now: true, ...ready },
			{ name: 'Fast', repository: 'Jundot/Fast', download_size_gb: 12, context_window: 32768, smarts_level: 3, tokens_per_second: 90, fits_now: true, ...ready },
		] });
		expect(document.body.querySelector('.modal-frame')?.classList.contains('opaque')).toBe(true);
		expect(document.body.querySelector('select')).toBeNull();
		const options = [...document.body.querySelectorAll<HTMLButtonElement>('[role="radiogroup"][aria-label="Sort models"] [role="radio"]')];
		expect(options.map((option) => [option.textContent, option.getAttribute('aria-checked')])).toEqual([['Quality', 'true'], ['Speed', 'false']]);
		const names = () => [...document.body.querySelectorAll('.model h3')].map((item) => item.textContent);
		expect(names()).toEqual(['Slow', 'Fast']);
		expect(options.map((option) => option.tabIndex)).toEqual([0, -1]);
		options[0].focus();
		options[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
		flushSync();
		expect(options[1].getAttribute('aria-checked')).toBe('true');
		expect(options.map((option) => option.tabIndex)).toEqual([-1, 0]);
		expect(document.activeElement).toBe(options[1]);
		expect(names()).toEqual(['Fast', 'Slow']);
		options[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
		flushSync();
		expect(document.activeElement).toBe(options[0]);
		expect(options[0].getAttribute('aria-checked')).toBe('true');
	});

	it('shows fitting models and hides those that exceed Mac room', async () => {
		const row = (name: string, resident_gb: number, fits_now: boolean) => ({ name, repository: `Jundot/${name}`, download_size_gb: 12, context_window: 32768, resident_bytes: resident_gb * 1024 ** 3, fits_now, ...ready });
		await render({ state: 'ready', memory_room_gb: 68, recommended_repository: 'Jundot/Small', models: [row('Small', 40, true), row('Huge', 163, false), row('Giant', 200, false)] });
		const rows = [...document.body.querySelectorAll('.model')];
		expect(rows.map((item) => item.querySelector('h3')?.textContent)).toEqual(['Small']);
		expect(rows[0].textContent).toContain('Recommended');
		expect(rows[0].querySelector('button')?.classList.contains('secondary')).toBe(true);
		expect(document.body.textContent).not.toContain('Needs more free memory');
	});

	it('shows an empty state when no model fits Mac room', async () => {
		const row = { name: 'Huge', repository: 'Jundot/Huge', download_size_gb: 12, context_window: 32768, resident_bytes: 163 * 1024 ** 3, fits_now: false, ...ready };
		// The backend's nothing-fits answer names no pick (test_provider_local_models covers that side).
		await render({ state: 'ready', memory_room_gb: 9.4, recommended_repository: null, models: [row] });
		expect(document.body.textContent).toContain('No recommended models fit this computer.');
		expect(document.body.querySelectorAll('.model')).toHaveLength(0);
		expect(document.body.textContent).not.toContain('Recommended');
	});

	it('marks no row Recommended when the backend names no pick', async () => {
		await render({ state: 'ready', models: [{ name: 'Qwen', repository: 'Jundot/Qwen', ...ready }] });
		expect(document.body.textContent).not.toContain('Recommended');
	});

	it('shows the disk requirement and disables download', async () => {
		await render({ state: 'ready', models: [{ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb: 12, context_window: 32768, disk: { state: 'insufficient' } }] });
		const download = [...document.body.querySelectorAll('button')].find((button) => button.textContent?.includes('Download'));
		expect(document.body.textContent).toContain('Needs more disk space');
		expect(download?.disabled).toBe(true);
	});

	it('shows quality and estimated speed in the metadata, with the disk line below it', async () => {
		await render({ state: 'ready', models: [{ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb: 16, context_window: 262144, quantization_bits: 4, smarts_level: 4, tokens_per_second: 41.6, disk: { state: 'insufficient', required_bytes: 31 * 1000 ** 3 } }] });
		const text = document.body.textContent ?? '';
		expect(text).toContain('16 GB · ~42 tokens/s');
		expect(text).toContain('Quality 4 of 5 · 256K context');
		expect(text.indexOf('256K context')).toBeLessThan(text.indexOf('Needs 31 GB of disk'));
	});

	it.each([
		[0.5, 'Quality 0.5 of 5', 0, 1],
		[2.5, 'Quality 2.5 of 5', 2, 1],
		[3.5, 'Quality 3.5 of 5', 3, 1],
		[5, 'Quality 5 of 5', 5, 0],
	])('shows quality %s as the exact labeled score', async (level, label) => {
		await render({ state: 'ready', models: [{ name: 'Qwen', repository: 'Jundot/Qwen', smarts_level: level, ...ready }] });
		expect(document.body.textContent).toContain(label);
	});

	it('shows the fitting model names as headings', async () => {
		await render({ state: 'ready', models: [
			{ name: 'Best', repository: 'Jundot/Best', fits_now: true, ...ready },
			{ name: 'Quick', repository: 'Jundot/Quick', fits_now: true, ...ready },
		] });
		expect([...document.body.querySelectorAll('.model h3')].map((heading) => heading.textContent)).toEqual(['Best', 'Quick']);
	});

	it('hides a model that exceeds Mac room and its download action', async () => {
		const row = (resident_bytes?: number, download_size_gb = 12) => ({ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb, context_window: 32768, resident_bytes, fits_now: false, ...ready });
		await render({ state: 'ready', memory_gb: 256, memory_room_gb: 123.4, recommended_repository: 'Jundot/Qwen', models: [row(174 * 1024 ** 3)] });
		expect(document.body.textContent).not.toContain('Needs 174 GB of memory');
		expect([...document.body.querySelectorAll('button')].some((button) => button.textContent?.includes('Download'))).toBe(false);
		expect(document.body.textContent).not.toContain('Recommended');
	});

	it('shows no memory line when the model fits, oMLX gives no figure, or only the download is large', async () => {
		const row = (resident_bytes?: number, download_size_gb = 12) => ({ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb, context_window: 32768, resident_bytes, fits_now: true, ...ready });
		await render({ state: 'ready', memory_room_gb: 123, models: [row(20 * 1024 ** 3)] });
		expect(host.textContent).not.toContain('of memory');
		await unmount(component!); host.remove();
		await render({ state: 'ready', memory_room_gb: 20, models: [row(undefined, 400)] });
		expect(host.textContent).not.toContain('of memory');
	});

	it('shows the initiating download loading until a failed request settles', async () => {
		await render({ state: 'ready', models: [
			{ name: 'Qwen', repository: 'Jundot/Qwen', ...ready },
			{ name: 'Other', repository: 'Jundot/Other', ...ready },
		] });
		let rejectRequest!: (reason: Error) => void;
		vi.stubGlobal('fetch', vi.fn(() => new Promise((_resolve, reject) => { rejectRequest = reject; })));
		const buttons = [...document.body.querySelectorAll<HTMLButtonElement>('button')].filter((button) => button.textContent?.trim() === 'Download');
		buttons[0].click(); flushSync();
		expect(buttons[0].classList.contains('loading')).toBe(true);
		expect(buttons.every((button) => button.disabled)).toBe(true);
		expect(buttons[1].classList.contains('loading')).toBe(false);
		rejectRequest(new Error('Start failed'));
		await vi.waitFor(() => expect(document.body.textContent).toContain('Start failed'));
		expect(buttons[0].classList.contains('loading')).toBe(false);
		expect(buttons.every((button) => !button.disabled)).toBe(true);
	});

	it.each([
		['Retry', true, true], ['Delete download', true, true], ['Cancel', false, true], ['Cancel', false, false],
	])('shows pending %s for a %s failure, listed=%s', async (action, failed, listed) => {
		host = document.createElement('div'); document.body.appendChild(host); token.set('test-token');
		let rejectRequest!: (reason: Error) => void;
		vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
			if (init?.method && init.method !== 'GET') return new Promise((_resolve, reject) => { rejectRequest = reject; });
			return Promise.resolve({ ok: true, json: async () => String(url).includes('/providers/sources')
				? { sources: [{ source: 'omlx', settings_download: { name: 'Qwen', repository: 'Jundot/Qwen', percent: 12, failed, downloaded: false } }] }
				: { state: 'ready', models: listed ? [{ name: 'Qwen', repository: 'Jundot/Qwen', ...ready }, { name: 'Other', repository: 'Jundot/Other', ...ready }] : [] } });
		}));
		component = mount(AddLocalModelSheet, { target: host, props: { onClose: vi.fn() } });
		await vi.waitFor(() => expect(document.body.textContent).toContain(action));
		const button = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent?.trim() === action)!;
		button.click(); flushSync();
		expect(button.classList.contains('loading')).toBe(true);
		expect([...document.body.querySelectorAll<HTMLButtonElement>('.model button')].every((item) => item.disabled)).toBe(true);
		rejectRequest(new Error('Request failed'));
		await vi.waitFor(() => expect(document.body.textContent).toContain(action === 'Cancel' ? 'Couldn’t cancel' : 'Request failed'));
		if (listed) expect(button.classList.contains('loading')).toBe(false);
	});

	it('tells the person how to retry a download that cannot start', async () => {
		await render({ state: 'ready', models: [{ name: 'Qwen', repository: 'Jundot/Qwen', ...ready }] });
		token.set(null);
		[...document.body.querySelectorAll('button')].find((button) => button.textContent?.includes('Download'))!.click();
		await tick(); await tick(); flushSync();
		expect(document.body.textContent).toContain("Couldn't start the download. Try again.");
	});
	it('offers cancellation when the active download is absent from an empty list', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (url: string) => ({
			ok: true,
			json: async () => String(url).includes('/providers/sources')
				? { sources: [{ source: 'omlx', settings_download: { name: 'Qwen', repository: 'x/Qwen', percent: 12, failed: false } }] }
				: { state: 'ready', models: [] },
		})));
		component = mount(AddLocalModelSheet, { target: host, props: { onClose: vi.fn() } });
		await vi.waitFor(() => expect(document.body.textContent).toContain('Qwen is downloading.'));
		expect(document.body.textContent).toContain('No recommended models fit this computer.');
		expect([...document.body.querySelectorAll('button')].some((button) => button.textContent?.trim() === 'Cancel' && !button.disabled)).toBe(true);
	});

	it('names the model already downloading before the person picks another, and offers no Download', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('test-token');
		vi.stubGlobal('fetch', vi.fn(async (url: string) => ({
			ok: true,
			json: async () => String(url).includes('/providers/sources')
				? { sources: [{ source: 'omlx', settings_download: { name: 'DeepSeek V4 Flash 0731', repository: 'x/DeepSeek', percent: 12, failed: false } }] }
				: { state: 'ready', models: [
					{ name: 'DeepSeek V4 Flash 0731', repository: 'x/DeepSeek', download_size_gb: 166, context_window: 262144, ...ready },
					{ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb: 12, context_window: 32768, ...ready },
				] },
		})));
		component = mount(AddLocalModelSheet, { target: host, props: { onClose: vi.fn() } });
		await vi.waitFor(() => expect(document.body.textContent).toContain('DeepSeek V4 Flash 0731 is downloading.'));
		const downloads = [...document.body.querySelectorAll<HTMLButtonElement>('button')].filter((button) => button.textContent?.trim() === 'Download');
		expect(downloads).toHaveLength(1);
		expect(downloads[0].disabled).toBe(true);
		const running = [...document.body.querySelectorAll('.model')].find((row) => row.textContent?.includes('DeepSeek V4 Flash 0731'));
		expect(running?.textContent).toContain('Downloading');
	});
});
