import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import NewChatFab from './NewChatFab.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

describe('NewChatFab mobile drawer visibility', () => {
	it('marks the FAB closed when the mobile sidebar drawer is closed', () => {
		component = mount(NewChatFab, {
			target: host,
			props: {
				activeProjects: [],
				onNewChat: vi.fn(),
				sidebarOpen: false,
			},
		});
		flushSync();

		const wrap = host.querySelector('.rail-newchat-wrap');
		expect(wrap?.classList.contains('sidebar-closed')).toBe(true);
	});
});
