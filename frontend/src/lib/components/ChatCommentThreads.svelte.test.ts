import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import ChatCommentThreads from './ChatCommentThreads.svelte';

vi.mock('$lib/stores/attentionDock.svelte', () => ({ reload: vi.fn().mockResolvedValue(undefined) }));

let host: HTMLElement;
let component: Record<string, unknown>;
afterEach(() => { if (component) unmount(component); host?.remove(); vi.unstubAllGlobals(); });

it('shows a comment and its shared answer in the thread, and leaves an unanswered comment without a New mark', () => {
	host = document.createElement('div'); document.body.appendChild(host);
	const base = { user_message_id: 'send', created_at: '2026-10-27T00:00:00Z', note: 'Why?', quote: 'selected text' };
	component = mount(ChatCommentThreads, { target: host, props: {
		messageId: 'target', threadId: 'thread', comments: [
			{ ...base, id: 'one', cid: 'c1.1', answers: [{ assistant_message_id: 'answer', text: 'Shared answer', created_at: '2026-10-27T00:01:00Z', unread: true }] },
			{ ...base, id: 'two', cid: 'c1.2', answers: [] },
		],
	} });
	flushSync();
	const threads = host.querySelectorAll('[data-chat-comment]');
	expect(threads).toHaveLength(2);
	expect(threads[0].textContent).toContain('Shared answer');
	expect(threads[0].textContent).toContain('New');
	expect(threads[1].textContent).not.toContain('Shared answer');
	expect(threads[1].textContent).not.toContain('New');
});
