import { expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import ChatCommentSelection from './ChatCommentSelection.svelte';

it('saves an edited reply before its surface can unmount during the outro', async () => {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	const host = document.createElement('div'); document.body.appendChild(host);
	const comment = { id: 'reply', messageId: 'message', seq: 1, from_off: 0, to_off: 4, quote: 'text', anchor_prefix: '', anchor_suffix: '', note: 'old', root_user_message_id: 'send', root_cid: 'c1.1' };
	const onChange = vi.fn();
	const component = mount(ChatCommentSelection, { target: host, props: { threadId: 'thread', comments: [comment], onChange, onSendReply: vi.fn() } });
	flushSync();
	await component.edit('reply');
	flushSync();
	const field = document.querySelector<HTMLTextAreaElement>('textarea')!;
	field.value = 'saved reply'; field.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
	[...document.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.trim() === 'Save')!.click();
	await unmount(component);
	await tick();
	expect(onChange).toHaveBeenCalledOnce();
	expect(onChange.mock.calls[0][0][0].note).toBe('saved reply');
	host.remove(); vi.unstubAllGlobals();
});

const reply = (id: string, note: string) => ({ id, messageId: 'message', seq: 1, from_off: 0, to_off: 4, quote: 'text', anchor_prefix: '', anchor_suffix: '', note, root_user_message_id: 'send', root_cid: 'c1.1' });
async function openTyped(width: number) {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	vi.stubGlobal('innerWidth', width);
	const host = document.createElement('div'); document.body.appendChild(host);
	const component = mount(ChatCommentSelection, { target: host, props: { threadId: 'thread', comments: [reply('one', 'old'), reply('two', 'second')], onChange: vi.fn(), onSendReply: vi.fn() } });
	flushSync();
	await component.edit('one');
	flushSync();
	const field = () => document.querySelector<HTMLTextAreaElement>('textarea');
	field()!.value = 'typed so far'; field()!.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
	return { host, component, field };
}

it.each([[1440, 700], [700, 1440]])('keeps typed text when the window resizes from %i to %i', async (from, to) => {
	const { host, component, field } = await openTyped(from);
	vi.stubGlobal('innerWidth', to);
	window.dispatchEvent(new Event('resize'));
	flushSync(); await tick(); flushSync();
	expect(field()?.value).toBe('typed so far');
	await unmount(component); host.remove(); vi.unstubAllGlobals();
});

it('does not carry typed text into another comment opened next', async () => {
	const { host, component, field } = await openTyped(1440);
	await component.edit('two');
	flushSync(); await tick(); flushSync();
	expect(field()?.value).toBe('second');
	await unmount(component); host.remove(); vi.unstubAllGlobals();
});
