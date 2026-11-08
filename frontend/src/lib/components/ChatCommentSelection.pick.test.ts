import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ChatCommentSelectionPickHost from './ChatCommentSelectionPickHost.test.svelte';
import { messages } from '$lib/stores/threads';
import { chatReplyDrafts } from './chatReplyDrafts.svelte';

// Where the column is up, the transcript's highlight is the control that picks its card, so the picked id
// has to reach the caller that shares it with the column. This walks the real surface through the same
// binding `ChatPanel` uses: a painted highlight, the click on it, then the first Escape.

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function setupSurface(): void {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	vi.stubGlobal('innerWidth', 1440);
	host = document.createElement('div');
	host.innerHTML = '<div class="messages"><div class="message assistant" data-message-id="m1"><div class="long-message-body">text more</div></div></div><div id="mount"></div>';
	document.body.append(host);
}

const picked = () => host.querySelector<HTMLElement>('[data-picked]')?.dataset.picked ?? null;

afterEach(() => {
	if (component) unmount(component as never);
	component = null;
	messages.set([]);
	host?.remove();
	vi.unstubAllGlobals();
});

it('gives the caller the comment its highlight picked, and the first Escape drops the pick', async () => {
	setupSurface();
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', chat_comments: [
		{ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] },
	] }] as never);
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	await vi.waitFor(() => expect(host.querySelector('.long-message-body [data-comment-ids="one"]')).not.toBeNull());
	host.querySelector<HTMLElement>('.long-message-body [data-comment-ids="one"]')!.click();
	flushSync();
	await tick();
	expect(picked()).toBe('one');
	// Nothing else opened: the thread is already on screen beside its text.
	expect(host.querySelector('[role="dialog"]')).toBeNull();
	document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
	flushSync();
	await tick();
	expect(picked()).toBe('');
});

it('hands a thread open in its popover to its card when the column comes up, keeping the unsent reply', async () => {
	setupSurface();
	messages.set([{ id: 'm1', thread_id: 'thread', role: 'assistant', seq: 1, chat_comments: [
		{ id: 'one', cid: 'c1', user_message_id: 'u1', quote: 'text', note: 'Why?', from_off: 0, to_off: 4, anchor_prefix: '', anchor_suffix: '', created_at: '2026-11-05T00:00:00Z', answers: [] },
	] }] as never);
	component = mount(ChatCommentSelectionPickHost, { target: host.querySelector('#mount')! });
	// Narrow: the highlight opens the thread in its popover.
	host.querySelector<HTMLButtonElement>('[data-toggle-margin]')!.click();
	flushSync();
	await vi.waitFor(() => expect(host.querySelector('.long-message-body [data-comment-ids="one"]')).not.toBeNull());
	host.querySelector<HTMLElement>('.long-message-body [data-comment-ids="one"]')!.click();
	flushSync();
	await tick();
	flushSync();
	const field = document.querySelector<HTMLTextAreaElement>('[role="dialog"] textarea')!;
	field.value = 'keep this draft';
	field.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();

	// Widen: the card takes the thread, so it is open once, and the reply typed in the popover is the card's.
	host.querySelector<HTMLButtonElement>('[data-toggle-margin]')!.click();
	flushSync();
	await tick();
	flushSync();
	expect(document.querySelector('[role="dialog"]')).toBeNull();
	expect(picked()).toBe('one');
	expect(chatReplyDrafts.get('one')).toBe('keep this draft');
	chatReplyDrafts.set('one', '');
});
