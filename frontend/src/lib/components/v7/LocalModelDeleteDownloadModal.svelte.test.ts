import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import LocalModelDeleteDownloadModal from './LocalModelDeleteDownloadModal.svelte';
import { token } from '$lib/stores/auth';
import { ctxAndSizeMeta } from '$lib/stores/providerModelUtils';

let component: ReturnType<typeof mount> | null = null;
let host: HTMLElement;

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host?.remove();
	vi.unstubAllGlobals();
	token.set(null);
});

describe('LocalModelDeleteDownloadModal', () => {
	it('states the size and deletes only after the danger confirmation', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		token.set('tok');
		const onDeleted = vi.fn(async () => {});
		const onClose = vi.fn();
		const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ ok: true }) }));
		vi.stubGlobal('fetch', fetchMock);
		component = mount(LocalModelDeleteDownloadModal, {
			target: host,
			props: { model: { id: 'org/model', name: 'Qwen', state: 'default', meta: '', caps: [], sizeBytes: 8 * 1024 ** 3 }, onDeleted, onClose },
		});
		flushSync();

		expect(document.body.textContent).toContain('8.0 GB');
		expect(document.body.textContent).toContain(ctxAndSizeMeta(undefined, 8 * 1024 ** 3));
		expect(document.body.textContent).not.toContain('You can download it again later.');
		expect(fetchMock).not.toHaveBeenCalled();
		[...document.body.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Delete download')!.click();
		await tick(); await tick();
		expect(fetchMock).toHaveBeenCalledWith('/api/providers/local/downloads/org%2Fmodel', expect.objectContaining({ method: 'DELETE' }));
		expect(onDeleted).toHaveBeenCalledOnce();
		expect(onClose).toHaveBeenCalledOnce();
	});

	it('omits the size when the model does not provide it', () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		component = mount(LocalModelDeleteDownloadModal, {
			target: host,
			props: { model: { id: 'org/model', name: 'Qwen', state: 'default', meta: '8 GB', caps: [] }, onDeleted: vi.fn(), onClose: vi.fn() },
		});
		flushSync();

		expect(document.body.textContent).toContain('Delete the download of Qwen');
		expect(document.body.textContent).not.toContain('8 GB');
	});
});
