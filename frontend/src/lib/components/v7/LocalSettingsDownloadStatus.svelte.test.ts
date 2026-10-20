import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LocalSettingsDownloadStatus from './LocalSettingsDownloadStatus.svelte';
import { token } from '$lib/stores/auth';
import { emit } from '$lib/ws/events';

let component: ReturnType<typeof mount> | null = null;
let host: HTMLElement;

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host?.remove();
	vi.unstubAllGlobals();
	token.set(null);
});

describe('LocalSettingsDownloadStatus', () => {
	it('shows the downloading row, then nothing once the bytes are on disk', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('tok');
		let flight: object = { repository: 'org/model', name: 'Model', percent: 40, failed: false };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sources: [{ source: 'omlx', settings_download: flight }] }) })));
		const statuses: boolean[] = [];
		component = mount(LocalSettingsDownloadStatus, { target: host, props: { onStatusChange: (has: boolean) => statuses.push(has) } });
		await tick(); await tick(); flushSync();
		expect(host.textContent).toContain('Downloading 40%');

		flight = { repository: 'org/model', name: 'Model', percent: null, failed: false, finishing: true };
		emit('models_updated', {});
		await vi.waitFor(() => expect(host.textContent).not.toContain('Downloading'));
		expect(host.querySelector('.mr')).toBeNull();
		expect(statuses.at(-1)).toBe(true);
	});

	it("shows the switch on or off as the backend's will_pin says, and off-limits either way", async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('tok');
		let willPin: boolean | undefined = true;
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sources: [{ source: 'omlx', settings_download: { repository: 'org/model', name: 'Model', size_bytes: 8 * 1024 ** 3, percent: 40, failed: false, will_pin: willPin } }] }) })));
		component = mount(LocalSettingsDownloadStatus, { target: host });
		await vi.waitFor(() => expect(host.querySelector('button.tg')).not.toBeNull());
		const toggle = () => host.querySelector('button.tg') as HTMLButtonElement;
		expect(toggle().classList.contains('on')).toBe(true);
		expect(toggle().disabled).toBe(true);

		willPin = false;
		emit('models_updated', {});
		await vi.waitFor(() => expect(toggle().classList.contains('on')).toBe(false));
		expect(toggle().disabled).toBe(true);
	});

	it('says the model is downloaded, never "Couldn\'t download", when only listing it failed', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('tok');
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sources: [{ source: 'omlx', settings_download: { repository: 'org/model', name: 'Model', failed: true, downloaded: true } }] }) })));
		component = mount(LocalSettingsDownloadStatus, { target: host });
		await tick(); await tick(); flushSync();
		expect(host.textContent).toContain("Downloaded, but couldn't be added. Retry to finish adding it.");
		expect(host.textContent).not.toContain("Couldn't download");
	});

	it('deletes a failed download and removes its row after refresh', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('tok');
		let sourcesReads = 0;
		const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
			if (init?.method === 'DELETE') return { ok: true, json: async () => ({ ok: true }) };
			sourcesReads += 1;
			return {
				ok: true,
				json: async () => ({
					sources: sourcesReads === 1 ? [{
						source: 'omlx',
						settings_download: { repository: 'org/model', name: 'Model', failed: true },
					}] : [{ source: 'omlx' }],
				}),
			};
		});
		vi.stubGlobal('fetch', fetchMock);
		component = mount(LocalSettingsDownloadStatus, { target: host });
		await tick(); await tick(); flushSync();

		const row = host.querySelector<HTMLElement>('.mr')!;
		row.focus();
		row.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'ContextMenu' }));
		flushSync();
		[...document.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Delete download')!.click();
		await tick(); await tick(); flushSync();

		expect(fetchMock).toHaveBeenCalledWith(
			'/api/providers/local/downloads/org%2Fmodel',
			expect.objectContaining({ method: 'DELETE' }),
		);
		await vi.waitFor(() => expect(host.querySelector('.mr')).toBeNull());
	});
});
