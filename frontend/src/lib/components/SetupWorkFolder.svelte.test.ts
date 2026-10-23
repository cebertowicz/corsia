import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import SetupWorkFolder from './SetupWorkFolder.svelte';

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;

async function settle() { await tick(); flushSync(); }
function button(label: string) { return Array.from(host.querySelectorAll('button')).find((item) => item.textContent?.trim() === label) as HTMLButtonElement; }

function render(choice: unknown) {
	host = document.createElement('div');
	document.body.appendChild(host);
	Object.defineProperty(window, '__TAURI_INTERNALS__', { configurable: true, value: { invoke: vi.fn(async () => choice) } });
	component = mount(SetupWorkFolder, { target: host, props: { folder: '~/CORSIA', changed: false, changing: false, notice: null } });
	flushSync();
}

afterEach(async () => {
	if (component) await unmount(component);
	host?.remove();
	delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
	vi.restoreAllMocks();
});

describe('SetupWorkFolder', () => {
	it('holds every protected choice until the person keeps it or uses the default', async () => {
		for (const [path, protected_folder] of [
			['~/Documents', { kind: 'personal_folder', folder: 'Documents' }],
			['~/Desktop', { kind: 'personal_folder', folder: 'Desktop' }],
			['~/Downloads', { kind: 'personal_folder', folder: 'Downloads' }],
			['~', { kind: 'personal_folder', folder: 'Home' }],
			['~/Library/Mobile Documents/com~apple~CloudDocs', { kind: 'personal_folder', folder: 'iCloud Drive' }],
			['~/Library', { kind: 'contains_personal_folder', folder: 'iCloud Drive' }],
		] as const) {
			render({ state: 'ready', path, protected_folder });
			button('Change').click(); await settle();
			expect(host.textContent).toContain('Agents open anything in the work folder without asking;');
			expect(button('Use ~/CORSIA')).toBeTruthy();
			expect(button('Keep this folder')).toBeTruthy();
			await unmount(component!); component = null; host.remove();
		}
	});

	it('uses ~/CORSIA or keeps the chosen folder only after its explicit button', async () => {
		render({ state: 'ready', path: '~/Documents', protected_folder: { kind: 'personal_folder', folder: 'Documents' } });
		button('Change').click(); await settle();
		button('Use ~/CORSIA').click(); await settle();
		expect(host.textContent).toContain('~/CORSIA');
		expect(button('Keep this folder')).toBeUndefined();

		await unmount(component!); component = null; host.remove();
		render({ state: 'ready', path: '~/Documents', protected_folder: { kind: 'personal_folder', folder: 'Documents' } });
		button('Change').click(); await settle();
		button('Keep this folder').click(); await settle();
		expect(host.textContent).toContain('~/Documents');
		expect(button('Use ~/CORSIA')).toBeUndefined();
	});

	it('does not steer an empty new folder inside Documents or a normal folder', async () => {
		for (const path of ['~/Documents/CORSIA', '~/Projects/CORSIA']) {
			render({ state: 'ready', path, protected_folder: null });
			button('Change').click(); await settle();
			expect(host.textContent).toContain(path);
			expect(button('Use ~/CORSIA')).toBeUndefined();
			expect(button('Keep this folder')).toBeUndefined();
			await unmount(component!); component = null; host.remove();
		}
	});
});
