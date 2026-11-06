import { afterEach, expect, it, vi } from 'vitest';
import { unreadTargets } from './chatUnreadTargets';
import type { Message } from '$lib/stores/threads';

afterEach(() => vi.unstubAllGlobals());

it('chooses the closest unread rendered highlight on each side', () => {
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	const root = document.createElement('div');
	root.innerHTML = ['far', 'near', 'below', 'read'].map(id => `<div data-message-id="${id}"><span role="button" data-comment-ids="${id}">${id}</span></div>`).join('');
	vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({ top: 100, bottom: 200 } as DOMRect);
	for (const [id, top, bottom] of [['far', 0, 10], ['near', 60, 80], ['below', 220, 240], ['read', 90, 95]] as const) {
		vi.spyOn(root.querySelector<HTMLElement>(`[data-comment-ids="${id}"]`)!, 'getBoundingClientRect').mockReturnValue({ top, bottom } as DOMRect);
	}
	const rows = ['far', 'near', 'below', 'read'].map(id => ({ id, thread_id: 't', chat_comments: [{ id, quote: id, answers: [{ author_kind: 'agent', unread: id !== 'read' }] }] })) as Message[];
	const result = unreadTargets(root, rows, 't');
	expect(result.above?.quote).toBe('near');
	expect(result.below?.quote).toBe('below');
	expect(result.count).toBe(3);
	expect(unreadTargets(root, rows, 'other')).toEqual({ above: null, below: null, count: 0 });
});

it('treats highlights behind the header and composer as outside the visible viewport', () => {
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	const host = document.createElement('div'); host.className = 'chat-content';
	const header = document.createElement('header'); header.className = 'shell-header';
	const root = document.createElement('div'); const composer = document.createElement('div'); composer.className = 'input-floater';
	root.innerHTML = '<div data-message-id="a"><span role="button" data-comment-ids="a">a</span></div><div data-message-id="b"><span role="button" data-comment-ids="b">b</span></div>';
	host.append(root, composer); document.body.append(header, host);
	vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({ top: 0, bottom: 300 } as DOMRect);
	vi.spyOn(header, 'getBoundingClientRect').mockReturnValue({ bottom: 60 } as DOMRect);
	vi.spyOn(composer, 'getBoundingClientRect').mockReturnValue({ top: 240 } as DOMRect);
	vi.spyOn(root.querySelector<HTMLElement>('[data-comment-ids="a"]')!, 'getBoundingClientRect').mockReturnValue({ top: 30, bottom: 50 } as DOMRect);
	vi.spyOn(root.querySelector<HTMLElement>('[data-comment-ids="b"]')!, 'getBoundingClientRect').mockReturnValue({ top: 250, bottom: 270 } as DOMRect);
	const rows = ['a', 'b'].map(id => ({ id, thread_id: 't', chat_comments: [{ id, quote: id, answers: [{ author_kind: 'agent', unread: true }] }] })) as Message[];
	expect(unreadTargets(root, rows, 't').above?.quote).toBe('a');
	expect(unreadTargets(root, rows, 't').below?.quote).toBe('b');
	header.remove(); host.remove();
});
