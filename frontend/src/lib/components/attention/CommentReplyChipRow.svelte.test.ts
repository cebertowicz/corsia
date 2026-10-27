import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import CommentReplyChipRow from './CommentReplyChipRow.svelte';
import type { AttentionQueue, CommentReplyItem } from '$lib/api/attentionQueue';
import { __reset } from '$lib/stores/attentionDock.svelte';
import { token } from '$lib/stores/auth';
import { activeThreadId } from '$lib/stores/threads';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let body: AttentionQueue;

function reply(id: string, title: string, options: { threadId?: string; unreadCount?: number; newestAt?: string } = {}): CommentReplyItem {
	return {
		detail: {
			title,
			unread_count: options.unreadCount ?? 1,
			newest_at: options.newestAt ?? '2026-10-24T15:00:00Z',
			destination: { pane: 'document-replies', id: `document-${id}`, path: `/tmp/${id}.md`, title },
			source: { thread_id: options.threadId ?? 'thread-1', thread_label: 'Thread' },
		},
	};
}

async function settle() {
	for (let attempt = 0; attempt < 8; attempt += 1) {
		await Promise.resolve();
		await tick();
		flushSync();
	}
}

beforeEach(() => {
	host = document.body;
	body = { current: null, count: 0, items: [] };
	__reset();
	token.set('test-bearer');
	activeThreadId.set('thread-1');
	vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => body })));
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.replaceChildren();
	__reset();
	token.set(null);
	activeThreadId.set(null);
	vi.unstubAllGlobals();
});

describe('CommentReplyChipRow', () => {
	it('coalesces only this chat’s unread replies and opens the newest document', async () => {
		const first = reply('one', 'launch-plan.md', { unreadCount: 2, newestAt: '2026-10-24T15:00:00Z' });
		const newest = reply('two', 'roadmap.md', { newestAt: '2026-10-24T16:00:00Z' });
		const otherChat = reply('three', 'private.md', { threadId: 'thread-2', newestAt: '2026-10-24T17:00:00Z' });
		body = { current: null, count: 0, items: [], reply_items: [first, newest, otherChat] };
		const open = vi.fn();
		component = mount(CommentReplyChipRow, { target: host, props: { onOpenDocument: open } });
		await (await import('$lib/stores/attentionDock.svelte')).reload();
		await settle();

		const chip = host.querySelector<HTMLButtonElement>('button.tile.chip');
		expect(chip).not.toBeNull();
		expect(chip?.textContent).toContain('3 new replies · 2 documents');
		expect(chip?.querySelector('.ct-new-dot')).not.toBeNull();
		expect(chip?.querySelector('.ct-new')?.textContent).toBe('New');
		expect(chip?.getAttribute('aria-label')).toBe('3 new replies · 2 documents, document');
		chip!.click();
		expect(open).toHaveBeenCalledWith(newest.detail.destination, 'thread-1');
	});

	it('sums separate workers for the same document in the visible chat', async () => {
		body = { current: null, count: 0, items: [], reply_items: [reply('same', 'launch-plan.md'), reply('same', 'launch-plan.md', { unreadCount: 2 })] };
		component = mount(CommentReplyChipRow, { target: host });
		await (await import('$lib/stores/attentionDock.svelte')).reload();
		await settle();
		expect(host.querySelector('button.tile.chip')?.textContent).toContain('3 new replies · launch-plan.md');
	});

	it('keeps the document name in the one-document chip and renders nothing for another chat', async () => {
		body = { current: null, count: 0, items: [], reply_items: [reply('other', 'private.md', { threadId: 'thread-2' })] };
		component = mount(CommentReplyChipRow, { target: host });
		await (await import('$lib/stores/attentionDock.svelte')).reload();
		await settle();
		expect(host.querySelector('.reply-chip-notice')).toBeNull();
		expect(host.querySelector('.ct-new-dot')).toBeNull();

		unmount(component);
		component = mount(CommentReplyChipRow, { target: host, props: {} });
		body = { current: null, count: 0, items: [], reply_items: [reply('one', 'launch-plan.md')] };
		await (await import('$lib/stores/attentionDock.svelte')).reload();
		await settle();
		expect(host.querySelector('button.tile.chip')?.textContent).toContain('1 new reply · launch-plan.md');
	});
});
