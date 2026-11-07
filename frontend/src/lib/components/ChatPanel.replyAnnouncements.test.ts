import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { withEmptyAttentionQueue } from '../__tests__/fixtures/attentionQueue';
import { token } from '$lib/stores/auth';
import { activeThreadId, messages } from '$lib/stores/threads';

let announce: (quote: string) => void = () => {};
vi.mock('$lib/chatCommentRefresh', async original => ({
	...(await original<typeof import('$lib/chatCommentRefresh')>()),
	watchChatCommentThreads: (_pageSize: number, onReply: (quote: string) => void) => { announce = onReply; return () => {}; },
}));

const { default: ChatPanel } = await import('./ChatPanel.svelte');
const originalFetch = globalThis.fetch;
let host: HTMLElement;
let component: Record<string, any> | null = null;

beforeEach(() => {
	vi.spyOn(console, 'error').mockImplementation(() => {});
	globalThis.fetch = withEmptyAttentionQueue(vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ thread_kind: 'chat' }) })) as unknown as typeof fetch);
	token.set('tok');
	activeThreadId.set(null);
	messages.set([]);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	globalThis.fetch = originalFetch;
});

describe('ChatPanel reply announcements', () => {
	it('adds a node for each new reply after the oldest is dropped', () => {
		component = mount(ChatPanel, { target: host, props: { onOpenLightbox: () => {} } });
		flushSync();
		const region = host.querySelector('.reply-announcements')!;
		for (const quote of ['one', 'two', 'three']) { announce(quote); flushSync(); }
		const firstThree = [...region.children];
		announce('four');
		flushSync();
		const nodes = [...region.children];
		expect(nodes.map(node => node.textContent)).toEqual(['New reply on “two”', 'New reply on “three”', 'New reply on “four”']);
		// aria-relevant="additions" announces inserted nodes only, so the newest reply must be a new node, not rewritten text.
		expect(firstThree).not.toContain(nodes[2]);
		expect(nodes.slice(0, 2)).toEqual(firstThree.slice(1));
	});
});
