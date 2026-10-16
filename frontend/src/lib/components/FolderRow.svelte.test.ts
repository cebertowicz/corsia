import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import FolderRow from './FolderRow.svelte';

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;

afterEach(async () => {
	if (component) await unmount(component);
	host?.remove();
});

function render(props: ConstructorParameters<typeof FolderRow>[0]['props']) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(FolderRow, { target: host, props });
	flushSync();
}

describe('FolderRow', () => {
	it('opens Change and leaves Continue eligible for a protected folder', () => {
		const change = vi.fn();
		render({ folder: '~/Documents/corsia', notice: { kind: 'protected', folder: 'Documents' }, onChange: change });

		expect(host.textContent).toContain('Work folder');
		expect(host.textContent).toContain('~/Documents/corsia');
		expect(host.textContent).toContain('macOS will ask before CORSIA can use Documents.');
		(host.querySelector('button') as HTMLButtonElement).click();
		expect(change).toHaveBeenCalledOnce();
	});

	it('shows the write reason and exposes its blocking state', () => {
		render({ folder: '/Volumes/Archive/corsia', notice: { kind: 'unwritable', reason: 'the disk is read-only' }, onChange: vi.fn() });

		expect(host.textContent).toContain("CORSIA can’t write here: the disk is read-only.");
		expect(host.querySelector('[data-folder-blocked="true"]')).not.toBeNull();
	});
});
