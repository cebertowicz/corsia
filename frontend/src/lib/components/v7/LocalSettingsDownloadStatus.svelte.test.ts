import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LocalSettingsDownloadStatus from './LocalSettingsDownloadStatus.svelte';
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

describe('LocalSettingsDownloadStatus', () => {
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
