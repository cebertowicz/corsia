import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/stores/networkServers', () => ({
	addNetworkServer: vi.fn(),
	parseModelIds: vi.fn(() => []),
}));
vi.mock('$lib/stores/providersV7', () => ({ loadHostGroups: vi.fn() }));

import AddProviderModal from './AddProviderModal.svelte';
import { providers } from '$lib/stores/providers';
import { token } from '$lib/stores/auth';

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;

function button(label: string) {
	return Array.from(document.body.querySelectorAll('button')).find((candidate) => candidate.textContent?.includes(label)) as HTMLButtonElement;
}

function stubLocalRow(row: Record<string, unknown>) {
	token.set('tok');
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', ...row }] }) })));
}

function render() {
	providers.set([]);
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(AddProviderModal, { target: host, props: { onClose: vi.fn(), onSelectCloud: vi.fn() } });
	flushSync();
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	token.set(null);
});

describe('AddProviderModal web-address copy', () => {
	it('uses web-address wording for endpoint validation and keyless setup', () => {
		render();
		button('Other provider by address').click();
		flushSync();

		expect(document.body.querySelector('input[placeholder="(leave empty when the web address needs no key)"]')).toBeTruthy();
		const address = document.body.querySelector<HTMLInputElement>('input[placeholder="https://host.example.com/compatible-mode/v1"]')!;
		address.value = 'not-a-web-address';
		address.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();
		button('Test & add').click();
		flushSync();
		expect(document.body.textContent).toContain('Enter the full web address, including https://');
	});
});

describe('AddProviderModal remote oMLX wording', () => {
	it('says another Mac on a Mac', async () => {
		stubLocalRow({ eligible: true });
		render();
		await new Promise((resolve) => setTimeout(resolve, 20)); flushSync();
		expect(button('oMLX on another Mac')).toBeTruthy();
		expect(document.body.textContent).not.toContain('oMLX on a Mac');
	});

	it('says a Mac on a server that is not macOS, and keeps the provider', async () => {
		stubLocalRow({ eligible: false, never_local: true });
		render();
		await vi.waitFor(() => expect(button('oMLX on a Mac')).toBeTruthy());
		expect(document.body.textContent).toContain('Use the local models oMLX runs on a Mac');
		expect(document.body.textContent).not.toContain('another Mac');
		button('oMLX on a Mac').click();
		flushSync();
		expect(document.body.textContent).toContain('Add the local models oMLX already runs on a Mac.');
	});
});
