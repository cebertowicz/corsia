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
	await tick(); await tick(); flushSync();
}

describe('AddLocalModelSheet', () => {
	it('shows the no-starter empty state when the recommender has no model', async () => {
		await render({ state: 'ready', sections: {} });
		expect(host.textContent).toContain('No recommended models fit this computer.');
		expect(host.textContent).not.toContain('Download');
	});

	it('uses the first recommended row as the primary download action', async () => {
		await render({ state: 'ready', sections: { best: [{ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb: 12, context_window: 32768, disk: { state: 'ready' } }], alternatives: [{ name: 'Other', repository: 'Jundot/Other', download_size_gb: 16, context_window: 32768, disk: { state: 'ready' } }] } });
		expect(host.textContent).toContain('Recommended');
		const downloads = [...host.querySelectorAll<HTMLButtonElement>('button')].filter((button) => button.textContent?.includes('Download'));
		expect(downloads).toHaveLength(2);
		expect(downloads[0].classList.contains('primary')).toBe(true);
		expect(downloads[1].classList.contains('chip')).toBe(true);
	});

	it('shows the disk requirement without a disabled download button', async () => {
		await render({ state: 'ready', sections: { best: [{ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb: 12, context_window: 32768, disk: { state: 'insufficient' } }] } });
		const download = [...host.querySelectorAll('button')].find((button) => button.textContent?.includes('Download'));
		expect(host.textContent).toContain('Needs more disk space');
		expect(download).toBeUndefined();
	});

	it('formats structured recommender facts and keeps the disk line below metadata', async () => {
		await render({ state: 'ready', sections: { best: [{ name: 'Qwen', repository: 'Jundot/Qwen', download_size_gb: 16, context_window: 262144, quantization_bits: 4, relative_speed: 0.85, disk: { state: 'insufficient', required_bytes: 31 * 1000 ** 3 } }] } });
		const text = host.textContent ?? '';
		expect(text).toContain('262k context · 4-bit · 16 GB · A bit slower');
		expect(text.indexOf('262k context')).toBeLessThan(text.indexOf('Needs 31 GB of disk'));
		expect(host.querySelector('.model-name')?.tagName).toBe('SPAN');
	});

	it('maps recommender sections to product labels and hides unknown keys', async () => {
		await render({ state: 'ready', sections: {
			best: [{ name: 'Best', repository: 'Jundot/Best', disk: { state: 'ready' } }],
			bigger: [{ name: 'Large', repository: 'Jundot/Large', disk: { state: 'ready' } }],
			unrecognized_section: [{ name: 'Unknown', repository: 'Jundot/Unknown', disk: { state: 'ready' } }],
		} });
		expect(host.textContent).toContain('Larger, less room for context');
		expect(host.textContent).toContain('More models');
		expect(host.textContent).not.toContain('unrecognized_section');
	});

	it('tells the person how to retry a download that cannot start', async () => {
		await render({ state: 'ready', sections: { best: [{ name: 'Qwen', repository: 'Jundot/Qwen', disk: { state: 'ready' } }] } });
		token.set(null);
		[...host.querySelectorAll('button')].find((button) => button.textContent?.includes('Download'))!.click();
		await tick(); await tick(); flushSync();
		expect(host.textContent).toContain("Couldn't start the download. Try again.");
	});
});
