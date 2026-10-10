import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ChatPanel from './ChatPanel.svelte';
import { activeThreadId, messages, threads } from '$lib/stores/threads';

const presence = vi.hoisted(() => ({
	startPresence: vi.fn(),
	stopPresence: vi.fn(),
}));

vi.mock('../ws/presence', () => presence);

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function renderChatPanel(): void {
	component = mount(ChatPanel, {
		target: host,
		props: { onOpenLightbox: () => {} },
	});
	flushSync();
}

beforeEach(() => {
	activeThreadId.set(null);
	threads.set([]);
	messages.set([]);
	presence.startPresence.mockReset();
	presence.stopPresence.mockReset();
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
});

describe('ChatPanel presence', () => {
	it('starts reporting for the main thread displayed on mount', () => {
		activeThreadId.set('thread-a');

		renderChatPanel();

		expect(presence.startPresence).toHaveBeenCalledWith('thread-a');
	});

	it('restarts reporting when the displayed main thread changes', () => {
		activeThreadId.set('thread-a');
		renderChatPanel();

		activeThreadId.set('thread-b');
		flushSync();

		expect(presence.startPresence).toHaveBeenNthCalledWith(1, 'thread-a');
		expect(presence.startPresence).toHaveBeenNthCalledWith(2, 'thread-b');
	});

	it('stops when no main thread is displayed and when the panel unmounts', () => {
		activeThreadId.set('thread-a');
		renderChatPanel();

		activeThreadId.set(null);
		flushSync();
		expect(presence.stopPresence).toHaveBeenCalledTimes(1);

		unmount(component!);
		component = null;
		expect(presence.stopPresence).toHaveBeenCalledTimes(2);
	});
});
