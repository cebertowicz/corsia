import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ChatThreadSheet from './ChatThreadSheet.svelte';

// A short popover must open a list at its top, with the title in view. Focus on the first Reply would scroll the title away, so the list's title takes focus.

let component: Record<string, unknown> | null = null;
let anchor: HTMLElement | null = null;

afterEach(() => {
	if (component) unmount(component as never);
	component = null;
	anchor?.remove();
	vi.unstubAllGlobals();
});

function comment(id: string) {
	return { id, cid: id, user_message_id: 'u1', quote: 'text', note: `note ${id}`, from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] };
}

async function open(listed: boolean): Promise<void> {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	anchor = document.createElement('span');
	document.body.append(anchor);
	component = mount(ChatThreadSheet, {
		target: document.body,
		props: {
			messageId: 'm1', threadId: 't1', comments: listed ? [comment('a'), comment('b')] : [comment('a')], listed, fromList: false, closing: false, narrow: false,
			anchor, bounds: null, onReply: async () => {}, onBack: () => {}, onClose: () => {}, onClosed: () => {},
		} as never,
	});
	flushSync();
	await tick();
	await new Promise((resolve) => setTimeout(resolve, 50));
}

it('focuses the list title, not the first Reply, when a list opens in a popover', async () => {
	await open(true);
	expect(document.activeElement?.textContent).toBe('2 comments');
});

it('leaves a single thread opening on its reply box', async () => {
	await open(false);
	expect(document.activeElement?.tagName).toBe('TEXTAREA');
});
