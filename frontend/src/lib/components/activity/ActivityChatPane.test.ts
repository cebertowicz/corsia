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
	const acted = vi.fn();
	const close = vi.fn();
	const fetcher = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => String(input).includes('/chats/')
		? new Response(JSON.stringify({ messages: [{ id: 'm', seq: 1, content: 'hidden answer tags', chat_visible_content: '**quoted** text', created_at: '2026-11-24T12:00:00Z', chat_comments: [{ id: 'one', cid: 'c1', user_message_id: 'u', from_off: 1, to_off: 7, quote: 'quoted', note: 'Why?', created_at: '2026-11-24T12:01:00Z', answers: [{ author_kind: 'agent', text: 'Because.', created_at: '2026-11-25T12:00:00Z' }] }] }] }), { status: 200 })
		: new Response('{}', { status: 200 }));
	vi.stubGlobal('fetch', fetcher);
	component = mount(ActivityChatPane, { target: host, props: { item: row, detail: row.detail as ActivityChatDetail, onActed: acted, onOpenChat: vi.fn(), onClose: close } });
	await vi.waitFor(() => expect(host.textContent).toContain('Because.'));
	expect(host.textContent).toContain('Why?');
	expect(host.querySelector('.message strong .cm-comment-wrap')?.textContent).toBe('quoted');
	expect(host.textContent).not.toContain('hidden answer tags');
	host.querySelector<HTMLButtonElement>('[aria-label="Close Planning"]')!.click();
	expect(close).toHaveBeenCalledOnce();
	[...host.querySelectorAll('button')].find(button => button.textContent?.trim() === 'Done')!.click();
	await vi.waitFor(() => expect(acted).toHaveBeenCalledOnce());
	const call = fetcher.mock.calls.find(([url]) => String(url).includes('/notifications/read'));
	expect(JSON.parse((call![1] as RequestInit).body as string).chats).toEqual([{ thread_id: 't', through: '2026-11-25T12:00:00Z' }]);
	await tick();
});
