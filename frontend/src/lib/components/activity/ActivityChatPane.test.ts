import { mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ActivityChatPane from './ActivityChatPane.svelte';
import type { ActivityChatDetail, ActivityQueueItem } from '$lib/api/activityQueue';
import { token } from '$lib/stores/auth';

let host: HTMLElement;
let component: ReturnType<typeof mount>;
const row = {
	key: 'chat-reply:t', external_id: 'chat-reply:t', renderer: 'chat', kind: 'chat', title: 'Planning',
	raised_at: '2026-11-25T12:00:00Z', source: { thread_id: 't', thread_label: 'Planning', project_id: null, project_label: null, parent_reviewed: false },
	open: null, rank: null, detail: { thread_id: 't', unread: 4, through: '2026-11-25T12:00:00Z' },
} as ActivityQueueItem;
beforeEach(() => { host = document.createElement('div'); document.body.append(host); token.set('test'); });
afterEach(() => { if (component) unmount(component); host.remove(); vi.unstubAllGlobals(); });

it('shows the answer and Done marks the displayed chat read', async () => {
	const done = vi.fn();
	const close = vi.fn();
	const fetcher = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => String(input).includes('/chats/')
		? new Response(JSON.stringify({ messages: [{ id: 'm', seq: 1, content: 'hidden answer tags', chat_visible_content: '**quoted** text', created_at: '2026-11-24T12:00:00Z', chat_comments: [{ id: 'one', cid: 'c1', user_message_id: 'u', from_off: 1, to_off: 7, quote: 'quoted', note: 'Why?', created_at: '2026-11-24T12:01:00Z', answers: [{ author_kind: 'agent', text: 'Because.', created_at: '2026-11-25T12:00:00Z' }] }] }] }), { status: 200 })
		: new Response('{}', { status: 200 }));
	vi.stubGlobal('fetch', fetcher);
	component = mount(ActivityChatPane, { target: host, props: { item: row, detail: row.detail as ActivityChatDetail, onDone: done, onOpenChat: vi.fn(), onClose: close } });
	await vi.waitFor(() => expect(host.textContent).toContain('Because.'));
	expect(host.textContent).toContain('Why?');
	expect(host.querySelector('.message strong .cm-comment-wrap')?.textContent).toBe('quoted');
	expect(host.textContent).not.toContain('hidden answer tags');
	host.querySelector<HTMLButtonElement>('[aria-label="Close Planning"]')!.click();
	expect(close).toHaveBeenCalledOnce();
	[...host.querySelectorAll('button')].find(button => button.textContent?.trim() === 'Done')!.click();
	await vi.waitFor(() => expect(done).toHaveBeenCalledWith('2026-11-25T12:00:00Z'));
	await tick();
});

it.each([1, 3])('names the jump control for %i unread replies', async (count) => {
	const answers = Array.from({ length: count }, (_, i) => ({
		author_kind: 'agent', text: `Answer ${i}`, created_at: '2026-11-25T12:00:00Z', unread: true,
		user_message_id: 'u', cid: 'c1', assistant_message_id: `a${i}`,
	}));
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ messages: [{
		id: 'm', seq: 1, content: 'text', created_at: '2026-11-24T12:00:00Z',
		chat_comments: [{ id: 'one', cid: 'c1', user_message_id: 'u', from_off: 0, to_off: 0,
			quote: '', note: 'Why?', created_at: '2026-11-24T12:01:00Z', answers }],
	}] }), { status: 200 })));
	component = mount(ActivityChatPane, { target: host, props: { item: row, detail: row.detail as ActivityChatDetail, onDone: vi.fn(), onOpenChat: vi.fn(), onClose: vi.fn() } });
	await vi.waitFor(() => expect(host.querySelector('[data-doc-jump] button')?.getAttribute('aria-label'))
		.toBe(`Go to ${count} new ${count === 1 ? 'reply' : 'replies'} below`));
});

it('renders threads with the same comment id from different messages', async () => {
	const comment = { cid: 'c1.1', from_off: 0, to_off: 0, quote: '', created_at: '2026-11-24T12:01:00Z', answers: [] };
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ messages: [{
		id: 'm', seq: 1, content: 'text', created_at: '2026-11-24T12:00:00Z',
		chat_comments: [{ ...comment, id: 'one', user_message_id: 'u1', note: 'First' },
			{ ...comment, id: 'two', user_message_id: 'u2', note: 'Second' }],
	}] }), { status: 200 })));
	component = mount(ActivityChatPane, { target: host, props: { item: row, detail: row.detail as ActivityChatDetail, onDone: vi.fn(), onOpenChat: vi.fn(), onClose: vi.fn() } });
	await vi.waitFor(() => expect(host.querySelectorAll('.conversation')).toHaveLength(2));
	expect(host.textContent).toContain('First');
	expect(host.textContent).toContain('Second');
});
