import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { activeThreadId, messages } from '$lib/stores/threads';
import { handleWsError } from '$lib/ws/handlersConnection';
import ChatCommentThreads from './ChatCommentThreads.svelte';
import { chatReplyDrafts } from './chatReplyDrafts.svelte';

const comment = { id: 'root', cid: 'c1', from_off: 0, to_off: 1, anchor_prefix: '', anchor_suffix: '', user_message_id: 'sender', created_at: '2026-10-27T00:00:00Z', note: 'Why?', quote: 'selected', answers: [] };
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const send = vi.fn();
function show() {
	host = document.createElement('div'); document.body.append(host);
	component = mount(ChatCommentThreads, { target: host, props: { messageId: 'target', threadId: 'thread', comments: [comment], onReply: send, keepReplyVisible: true } });
	flushSync();
}
afterEach(() => { if (component) unmount(component as never); component = null; host?.remove(); chatReplyDrafts.set('root', ''); activeThreadId.set(null); messages.set([]); send.mockReset(); });

it('keeps a rejected reply across a thread switch, then clears it on accepted retry', async () => {
	activeThreadId.set('thread'); send.mockReturnValueOnce('one').mockReturnValueOnce('two'); show();
	const field = host.querySelector<HTMLTextAreaElement>('textarea')!;
	field.value = 'keep my reply'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
	host.querySelector<HTMLButtonElement>('[data-send-reply]')!.click(); await tick(); flushSync();
	expect(chatReplyDrafts.reply('root')?.inputId).toBe('one');
	unmount(component! as never); component = null; host.remove(); activeThreadId.set('other');
	handleWsError({ thread_id: 'thread', input_id: 'one', message: 'Please try again.' });
	expect(chatReplyDrafts.get('root')).toBe('keep my reply');
	activeThreadId.set('thread'); show();
	expect(host.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('keep my reply');
	expect(host.querySelector('[role="alert"]')?.textContent).toContain('Please try again');
	host.querySelector<HTMLButtonElement>('[data-send-reply]')!.click(); await tick(); flushSync();
	expect(chatReplyDrafts.reply('root')?.inputId).toBe('two');
	chatReplyDrafts.settle('thread', 'two', 'accepted'); await tick(); flushSync();
	expect(chatReplyDrafts.get('root')).toBe('');
});
