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
	it('explains a protected folder and requires a deliberate default or keep choice', () => {
		const change = vi.fn(), useDefault = vi.fn(), keepProtected = vi.fn();
		render({ folder: '~/Documents/corsia', notice: { kind: 'protected', folder: 'Documents' }, onChange: change, onUseDefault: useDefault, onKeepProtected: keepProtected });

		expect(host.textContent).toContain('Work folder');
		expect(host.textContent).toContain('~/Documents/corsia');
		expect(host.textContent).toContain('Agents work freely inside your work folder. Your personal files are safer outside it, where CORSIA asks before opening them.');
		(Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Use ~/CORSIA') as HTMLButtonElement).click();
		(Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Keep this folder') as HTMLButtonElement).click();
		expect(useDefault).toHaveBeenCalledOnce();
		expect(keepProtected).toHaveBeenCalledOnce();
	});

	it('shows the write reason and exposes its blocking state', () => {
		render({ folder: '/Volumes/Archive/corsia', notice: { kind: 'unwritable', reason: 'the disk is read-only' }, onChange: vi.fn() });

		expect(host.textContent).toContain('CORSIA can’t write to this folder. Choose another one.');
		expect(host.querySelector('[data-folder-blocked="true"]')).not.toBeNull();
	});
});
