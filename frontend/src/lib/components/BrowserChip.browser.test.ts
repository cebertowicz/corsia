import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import BrowserChip from './BrowserChip.svelte';
import * as productEvents from '$lib/product_events';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function render(chip: { host: string | null }, threadId = 't1'): HTMLButtonElement {
	if (component) unmount(component);
	component = mount(BrowserChip, { target: host, props: { chip, threadId } });
	flushSync();
	return host.querySelector<HTMLButtonElement>('button')!;
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	threadWorkspaces.set({});
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
});

describe('BrowserChip', () => {
	it('is a real button that shows the host and says what a click does', () => {
		const button = render({ host: 'github.com' });

		expect(button.tagName).toBe('BUTTON');
		expect(button.type).toBe('button');
		expect(button.querySelector('.title')?.textContent).toBe('github.com');
		expect(button.hasAttribute('aria-label')).toBe(false);
		expect(button.getAttribute('title')).toBe('Reopen the shared browser');
		expect(button.querySelector('.ic svg')).not.toBeNull();
	});

	it('says Shared browser when the open named no host', () => {
		expect(render({ host: null }).querySelector('.title')?.textContent).toBe('Shared browser');
	});

	it('shows the live browser in its thread on click and reports one product event', () => {
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		render({ host: 'github.com' }, 'browser-thread').click();

		const ws = get(threadWorkspaces)['browser-thread'];
		expect(ws.tabs.map((tab) => tab.kind)).toEqual(['browser']);
		expect(ws).toMatchObject({ visible: true, activeSurface: 'artifact', activeTabId: ws.tabs[0].instanceId });
		expect(spy.mock.calls).toEqual([['browser_chip', 'pane_open']]);
	});

	it('does not let the click reach the chat column', () => {
		const button = render({ host: 'github.com' });
		const reached = vi.fn();
		// Svelte listens at the mount target, so the column stands one element above it.
		host.parentElement!.addEventListener('click', reached);

		button.click();

		expect(reached).not.toHaveBeenCalled();
	});
});
