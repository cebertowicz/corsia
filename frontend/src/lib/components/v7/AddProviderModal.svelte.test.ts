import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/stores/networkServers', () => ({
	addNetworkServer: vi.fn(),
	parseModelIds: vi.fn(() => []),
}));
vi.mock('$lib/stores/providersV7', () => ({ loadHostGroups: vi.fn() }));

import AddProviderModal from './AddProviderModal.svelte';
import { providers } from '$lib/stores/providers';

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;

function button(label: string) {
	return Array.from(document.body.querySelectorAll('button')).find((candidate) => candidate.textContent?.includes(label)) as HTMLButtonElement;
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
