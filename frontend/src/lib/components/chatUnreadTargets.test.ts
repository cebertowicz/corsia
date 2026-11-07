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
	const rows = ['far', 'near', 'below', 'read'].map(id => ({ id, seq: 1, thread_id: 't', chat_comments: [{ id, cid: `c-${id}`, user_message_id: `u-${id}`, quote: id, answers: [{ author_kind: 'agent', unread: id !== 'read' }] }] })) as Message[];
	const result = unreadTargets(root, rows, 't');
	expect(result.above?.quote).toBe('near');
	expect(result.below?.quote).toBe('below');
	expect(result.count).toBe(3);
	expect(unreadTargets(root, rows, 'other')).toEqual({ above: null, below: null, count: 0 });
});

it('lands on the thread under a message whose text no longer matches the comment', () => {
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	const root = document.createElement('div');
	// No highlight was painted: the comment's quote left the message, so only the detached thread is there to land on.
	root.innerHTML = '<div data-message-id="drifted"><div class="chat-comment-thread" tabindex="-1" data-comment-uuid="drifted" data-chat-comment="c-drifted">the answer</div></div>';
	vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({ top: 100, bottom: 200 } as DOMRect);
	vi.spyOn(root.querySelector<HTMLElement>('[data-comment-uuid="drifted"]')!, 'getBoundingClientRect').mockReturnValue({ top: 40, bottom: 70 } as DOMRect);
	const rows = [{ id: 'drifted', seq: 7, thread_id: 't', chat_comments: [{ id: 'drifted', cid: 'c-drifted', user_message_id: 'u-1', quote: 'gone text', answers: [{ author_kind: 'agent', unread: true }] }] }] as unknown as Message[];
	const result = unreadTargets(root, rows, 't');
	expect(result.above?.quote).toBe('gone text');
	// The target carries the jump's destination, so the one landing path can reach a page that is not loaded yet.
	expect(result.above?.destination).toEqual({ pane: 'chat-replies', id: 'drifted', seq: 7, cid: 'c-drifted', user_message_id: 'u-1' });
	expect(result.count).toBe(1);
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
	const rows = ['a', 'b'].map(id => ({ id, seq: 1, thread_id: 't', chat_comments: [{ id, cid: `c-${id}`, user_message_id: `u-${id}`, quote: id, answers: [{ author_kind: 'agent', unread: true }] }] })) as Message[];
	expect(unreadTargets(root, rows, 't').above?.quote).toBe('a');
	expect(unreadTargets(root, rows, 't').below?.quote).toBe('b');
	header.remove(); host.remove();
});

it('points at an unread reply inside a collapsed run, on its side of the first row in view', () => {
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	const root = document.createElement('div');
	// The commented message is loaded but its run is collapsed, so no row of it is drawn; the jumper opens the run.
	root.innerHTML = '<div data-message-id="shown" data-message-seq="10">shown</div>';
	vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({ top: 100, bottom: 200 } as DOMRect);
	vi.spyOn(root.querySelector<HTMLElement>('[data-message-id="shown"]')!, 'getBoundingClientRect').mockReturnValue({ top: 120, bottom: 160 } as DOMRect);
	const comment = (seq: number) => [{ id: 'run', seq, thread_id: 't', chat_comments: [{ id: 'run', cid: 'c-run', user_message_id: 'u-run', quote: 'in the run', answers: [{ author_kind: 'agent', unread: true }] }] }] as unknown as Message[];
	const earlier = unreadTargets(root, comment(3), 't');
	expect(earlier.above?.destination).toEqual({ pane: 'chat-replies', id: 'run', seq: 3, cid: 'c-run', user_message_id: 'u-run' });
	expect(earlier.count).toBe(1);
	expect(unreadTargets(root, comment(12), 't').below?.quote).toBe('in the run');
});

it('finds the highlight in the text row when the message also draws an activity row first', () => {
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	const root = document.createElement('div');
	root.innerHTML = '<div data-message-id="m">activity</div><div data-message-id="m"><span role="button" data-comment-ids="m">text</span></div>';
	vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({ top: 100, bottom: 200 } as DOMRect);
	vi.spyOn(root.querySelector<HTMLElement>('[data-comment-ids="m"]')!, 'getBoundingClientRect').mockReturnValue({ top: 20, bottom: 40 } as DOMRect);
	const rows = [{ id: 'm', seq: 4, thread_id: 't', chat_comments: [{ id: 'm', cid: 'c-m', user_message_id: 'u-m', quote: 'text', answers: [{ author_kind: 'agent', unread: true }] }] }] as unknown as Message[];
	expect(unreadTargets(root, rows, 't').above).toMatchObject({ quote: 'text', distance: 60 });
});
