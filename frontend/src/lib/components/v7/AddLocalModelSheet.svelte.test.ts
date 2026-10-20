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
	const tryAgain = () => [...document.body.querySelectorAll('button')].some((button) => button.textContent?.includes('Try again'));

	it('shows the backend sentence for a failed check and offers Try again, since asking again can help', async () => {
		await renderFailure(503, "Couldn't check which models fit this computer.");
		expect(tryAgain()).toBe(true);
	});

	it('shows a cause the person must fix first without a Try again that cannot succeed', async () => {
		await renderFailure(409, "Local models aren't set up. Select Set up to continue.");
		expect(tryAgain()).toBe(false);
	});

	it('shows the update message from oMLX without Try again', async () => {
		await render({ state: 'update_required', message: 'Update oMLX to 0.4.2 or later to add a local model.', models: [] });
		expect(document.body.textContent).toContain('Update oMLX to 0.4.2 or later to add a local model.');
		expect(tryAgain()).toBe(false);
	});

	it('names a memory need from the backend verdict alone, never its own comparison', async () => {
		const row = (name: string, fits_now: boolean) => ({ name, repository: `Jundot/${name}`, resident_bytes: 20.004 * 1024 ** 3, fits_now, ...ready });
		await render({ state: 'ready', memory_room_gb: 20, models: [row('Fits', true), row('Short', false)] });
		const rows = [...document.body.querySelectorAll('.model-row')];
		expect(rows[0].textContent).not.toContain('of memory');
		expect(rows[1].textContent).toContain('of memory');
	});

	it('shows the no-starter empty state when the recommender has no model', async () => {
		await render({ state: 'ready', models: [] });
		expect(document.body.textContent).toContain('No recommended models fit this computer.');
		expect(document.body.textContent).not.toContain('Download');
	});

	it('uses the row setup would pick as the primary download action', async () => {
		await render({ state: 'ready', recommended_repository: 'Jundot/Qwen', models: [
			{ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb: 12, context_window: 32768, fits_now: true, ...ready },
			{ name: 'Other', repository: 'Jundot/Other', download_size_gb: 16, context_window: 32768, fits_now: true, ...ready },
		] });
		expect(document.body.textContent).toContain('Recommended');
		const downloads = [...document.body.querySelectorAll<HTMLButtonElement>('button')].filter((button) => button.textContent?.includes('Download'));
		expect(downloads).toHaveLength(2);
		expect(downloads[0].classList.contains('primary')).toBe(true);
		expect(downloads[1].classList.contains('chip')).toBe(true);
	});

	it('lists models that fit now first, then one heading over those that need more free memory', async () => {
		const row = (name: string, resident_gb: number, fits_now: boolean) => ({ name, repository: `Jundot/${name}`, download_size_gb: 12, context_window: 32768, resident_bytes: resident_gb * 1024 ** 3, fits_now, ...ready });
		await render({ state: 'ready', memory_room_gb: 68, recommended_repository: 'Jundot/Small', models: [row('Small', 40, true), row('Huge', 163, false), row('Giant', 200, false)] });
		const rows = [...document.body.querySelectorAll('.model-row')];
		expect(rows.map((item) => item.querySelector('.model-name')?.textContent)).toEqual(['Small', 'Huge', 'Giant']);
		expect(rows[0].textContent).toContain('Recommended');
		expect(rows[0].querySelector('button')?.classList.contains('primary')).toBe(true);
		expect(rows[1].textContent).not.toContain('Recommended');
		expect(rows[1].querySelector('button')?.classList.contains('chip')).toBe(true);
		expect([...document.body.querySelectorAll('h3')].map((heading) => heading.textContent)).toEqual(['Needs more free memory']);
		expect(document.body.textContent).not.toContain('Nothing fits');
	});

	it('says nothing fits when every model needs more free memory', async () => {
		const row = { name: 'Huge', repository: 'Jundot/Huge', download_size_gb: 12, context_window: 32768, resident_bytes: 163 * 1024 ** 3, fits_now: false, ...ready };
		// The backend's nothing-fits answer names no pick (test_provider_local_models covers that side).
		await render({ state: 'ready', memory_room_gb: 9.4, recommended_repository: null, models: [row] });
		expect(document.body.textContent).toContain('Nothing fits the 9 GB free for a model now.');
		expect([...document.body.querySelectorAll('h3')].map((heading) => heading.textContent)).toEqual(['Needs more free memory']);
		expect(document.body.textContent).not.toContain('Recommended');
	});

	it('marks no row Recommended when the backend names no pick', async () => {
		await render({ state: 'ready', models: [{ name: 'Qwen', repository: 'Jundot/Qwen', ...ready }] });
		expect(document.body.textContent).not.toContain('Recommended');
	});

	it('shows the disk requirement without a disabled download button', async () => {
		await render({ state: 'ready', models: [{ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb: 12, context_window: 32768, disk: { state: 'insufficient' } }] });
		const download = [...document.body.querySelectorAll('button')].find((button) => button.textContent?.includes('Download'));
		expect(document.body.textContent).toContain('Needs more disk space');
		expect(download).toBeUndefined();
	});

	it('shows quality dots and the estimated speed in the metadata, with the disk line below it', async () => {
		await render({ state: 'ready', models: [{ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb: 16, context_window: 262144, quantization_bits: 4, smarts_level: 4, tokens_per_second: 41.6, disk: { state: 'insufficient', required_bytes: 31 * 1000 ** 3 } }] });
		const text = document.body.textContent ?? '';
		expect(text).toContain('262k context\u00a0·\u00a04-bit\u00a0·\u00a016 GB\u00a0·\u00a0~42 tokens/s est.');
		expect(text.indexOf('262k context')).toBeLessThan(text.indexOf('Needs 31 GB of disk'));
		const quality = document.body.querySelector('.quality');
		expect(quality?.getAttribute('aria-label')).toBe('Quality 4 of 5');
		expect(quality?.querySelectorAll('.dot.on')).toHaveLength(4);
		expect(document.body.querySelector('.model-name')?.tagName).toBe('SPAN');
	});

	it.each([
		[0.5, 'Quality 0.5 of 5', 0, 1],
		[2.5, 'Quality 2.5 of 5', 2, 1],
		[3.5, 'Quality 3.5 of 5', 3, 1],
		[5, 'Quality 5 of 5', 5, 0],
	])('shows quality %s as whole and half dots, never rounded up', async (level, label, whole, half) => {
		await render({ state: 'ready', models: [{ name: 'Qwen', repository: 'Jundot/Qwen', smarts_level: level, ...ready }] });
		const quality = document.body.querySelector('.quality');
		expect(quality?.getAttribute('aria-label')).toBe(label);
		expect(quality?.querySelectorAll('.dot.on')).toHaveLength(whole);
		expect(quality?.querySelectorAll('.dot.half')).toHaveLength(half);
	});

	it('shows no section headings other than the memory group', async () => {
		await render({ state: 'ready', models: [
			{ name: 'Best', repository: 'Jundot/Best', fits_now: true, ...ready },
			{ name: 'Quick', repository: 'Jundot/Quick', fits_now: true, ...ready },
		] });
		expect(document.body.querySelectorAll('h3')).toHaveLength(0);
	});

	it('says what a model needs in memory when that exceeds the room free for a model, and keeps Download', async () => {
		const row = (resident_bytes?: number, download_size_gb = 12) => ({ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb, context_window: 32768, resident_bytes, fits_now: false, ...ready });
		await render({ state: 'ready', memory_gb: 256, memory_room_gb: 123.4, recommended_repository: 'Jundot/Qwen', models: [row(174 * 1024 ** 3)] });
		expect(document.body.textContent).toContain('Needs 174 GB of memory · 123 GB free for a model now');
		expect([...document.body.querySelectorAll('button')].some((button) => button.textContent?.includes('Download') && !button.disabled)).toBe(true);
		expect(document.body.textContent).toContain('Recommended');
	});

	it('shows no memory line when the model fits, oMLX gives no figure, or only the download is large', async () => {
		const row = (resident_bytes?: number, download_size_gb = 12) => ({ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb, context_window: 32768, resident_bytes, fits_now: true, ...ready });
		await render({ state: 'ready', memory_room_gb: 123, models: [row(20 * 1024 ** 3)] });
		expect(host.textContent).not.toContain('of memory');
		await unmount(component!); host.remove();
		await render({ state: 'ready', memory_room_gb: 20, models: [row(undefined, 400)] });
		expect(host.textContent).not.toContain('of memory');
	});

	it('tells the person how to retry a download that cannot start', async () => {
		await render({ state: 'ready', models: [{ name: 'Qwen', repository: 'Jundot/Qwen', ...ready }] });
		token.set(null);
		[...document.body.querySelectorAll('button')].find((button) => button.textContent?.includes('Download'))!.click();
		await tick(); await tick(); flushSync();
		expect(document.body.textContent).toContain("Couldn't start the download. Try again.");
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
		const running = [...document.body.querySelectorAll('.model-row')].find((row) => row.textContent?.includes('DeepSeek V4 Flash 0731'));
		expect(running?.textContent).toContain('Downloading');
	});
});
