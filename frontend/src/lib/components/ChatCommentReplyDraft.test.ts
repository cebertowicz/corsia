import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { activeThreadId, messages } from '$lib/stores/threads';
import { handleWsError } from '$lib/ws/handlersConnection';
import { handleMessage } from '$lib/ws/handlers';
import { sendMessage } from '$lib/ws/send';
import { setWs } from '$lib/ws/state';
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
	chatReplyDrafts.release('thread', 'two'); await tick(); flushSync();
	expect(chatReplyDrafts.get('root')).toBe('');
});

it('keeps a reply that was stored and then failed, with its Retry, in its thread', async () => {
	activeThreadId.set('thread'); messages.set([]);
	setWs({ readyState: WebSocket.OPEN, send: () => {} } as unknown as WebSocket);
	// The real send path: the reply's row is what its thread reads the failure from.
	send.mockImplementation((text: string) => sendMessage('thread', '', undefined, [], undefined, 'apply_now',
		[{ id: 'reply-1', seq: 1, from_off: 0, to_off: 1, quote: 'selected', anchor_prefix: '', anchor_suffix: '', note: text, root_user_message_id: 'sender', root_cid: 'c1' }]));
	show();
	const field = host.querySelector<HTMLTextAreaElement>('textarea')!;
	field.value = 'keep my reply'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
	host.querySelector<HTMLButtonElement>('[data-send-reply]')!.click(); await tick(); flushSync();
	const inputId = send.mock.results[0].value as string;
	handleMessage({ type: 'input_queued', queue_kind: 'messages', thread_id: 'thread', input_id: inputId, message_id: 'stored-1', delivery_state: 'failed', delivery_error: 'Delivery failed.' }, () => {});
	await tick(); flushSync();
	const failure = host.querySelector('.failed-reply');
	expect(failure?.textContent).toContain('keep my reply');
	expect([...(failure?.querySelectorAll('button') ?? [])].map(button => button.textContent)).toContain('Retry');
	setWs(null as unknown as WebSocket);
});
