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
	it('explains an existing-contents warning and requires a deliberate default or keep choice', () => {
		const change = vi.fn(), useDefault = vi.fn(), keepFolder = vi.fn();
		render({ folder: '~/Documents/corsia', notice: { kind: 'warning', warning: { kind: 'contains_files' } }, onChange: change, onUseDefault: useDefault, onKeepFolder: keepFolder });

		expect(host.textContent).toContain('Work folder');
		expect(host.textContent).toContain('~/Documents/corsia');
		expect(host.textContent).toContain('This folder already holds files. Agents open anything in the work folder without asking; outside it, CORSIA asks you first.');
		(Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Use ~/CORSIA') as HTMLButtonElement).click();
		(Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Keep this folder') as HTMLButtonElement).click();
		expect(useDefault).toHaveBeenCalledOnce();
		expect(keepFolder).toHaveBeenCalledOnce();
	});

	it('names the conditional macOS access request only for a personal folder', () => {
		render({ folder: '~/Documents', notice: { kind: 'warning', warning: { kind: 'personal_folder', folder: 'Documents' } }, onChange: vi.fn() });

		expect(host.textContent).toContain('macOS may ask to let CORSIA use it.');
	});

	it('shows the write reason and exposes its blocking state', () => {
		render({ folder: '/Volumes/Archive/corsia', notice: { kind: 'unwritable', reason: 'the disk is read-only' }, onChange: vi.fn() });

		expect(host.textContent).toContain('CORSIA can’t write to this folder. Choose another one.');
		expect(host.querySelector('[data-folder-blocked="true"]')).not.toBeNull();
	});

	it('blocks a folder CORSIA cannot inspect', () => {
		render({ folder: '/Volumes/Archive/corsia', notice: { kind: 'unreadable', reason: 'permission denied' }, onChange: vi.fn() });

		expect(host.textContent).toContain('CORSIA can’t inspect this folder. Choose another one.');
		expect(host.querySelector('[data-folder-blocked="true"]')).not.toBeNull();
	});
});
