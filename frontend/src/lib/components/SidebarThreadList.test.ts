import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';

import SidebarThreadList from './SidebarThreadList.svelte';
import { WINDOW_THRESHOLD } from './sidebarWindow';
import { threads } from '$lib/stores/threads';
import { uiScope } from '$lib/stores/uiScope';

let host: HTMLElement | null = null;
let component: Record<string, unknown> | null = null;

function render(count: number) {
	threads.set(Array.from({ length: count }, (_, i) => ({
		id: `t${i}`, user_id: 'u', profile: 'personal', title: `Thread ${i}`, model: null,
		created_at: new Date(Date.now() - i * 1000).toISOString(), updated_at: new Date(Date.now() - i * 1000).toISOString(),
	})));
	uiScope.set(null);
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(SidebarThreadList, {
		target: host,
		props: { onSelectThread: () => {}, onDeleteThread: () => {}, onOpenContextMenu: () => {} },
	});
	flushSync();
	return host;
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	host = null;
	threads.set([]);
});

describe('SidebarThreadList list semantics', () => {
	it('is a list of listitems with no position attributes at or below the window threshold', () => {
		const root = render(WINDOW_THRESHOLD);
		expect(root.querySelector('[role="list"]')).not.toBeNull();
		expect(root.querySelectorAll('[role="listitem"]').length).toBe(WINDOW_THRESHOLD);
		expect(root.querySelector('[aria-posinset]')).toBeNull();
	});

	it('keeps the same structure above the threshold and adds position and set size', () => {
		const root = render(WINDOW_THRESHOLD + 1);
		expect(root.querySelector('[role="list"]')).not.toBeNull();
		const items = root.querySelectorAll('[role="listitem"]');
		expect(items.length).toBeGreaterThan(0);
		expect(items.length).toBeLessThan(WINDOW_THRESHOLD + 1);
		expect(items[0].getAttribute('aria-posinset')).toBe('1');
		expect(items[0].getAttribute('aria-setsize')).toBe(String(WINDOW_THRESHOLD + 1));
	});
});
