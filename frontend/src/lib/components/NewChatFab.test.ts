import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { readable } from 'svelte/store';
import NewChatFab from './NewChatFab.svelte';

// A phone with a thread waiting on the person: the menu rows and the header's menu toggle carry
// the needs-you dot. The New chat button must not read as an unread marker, whatever the menu holds.
vi.mock('$lib/stores/threadPlanStates', () => ({ listedNeedsYou: readable(true) }));
vi.mock('$lib/stores/viewport', () => ({ isMobile: readable(true) }));

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

describe('NewChatFab needs-you dot', () => {
	it('carries no needs-you dot or label on a phone while a thread needs the person', () => {
		component = mount(NewChatFab, {
			target: host,
			props: { activeProjects: [], onNewChat: vi.fn(), sidebarOpen: true },
		});
		flushSync();

		const button = host.querySelector('button.rail-newchat');
		expect(button?.getAttribute('aria-label')).toBe('New chat');
		expect(host.querySelector('[data-testid="fab-needs-you-dot"]')).toBeNull();
	});
});
