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
	const component = mount(ChatCommentSelection, { target: host, props: { threadId: 'thread', comments: [comment], onChange } });
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

async function openTyped(width: number, text: string | null = 'typed so far') {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
	vi.stubGlobal('cancelAnimationFrame', () => {});
	vi.stubGlobal('CSS', { escape: (value: string) => value });
	vi.stubGlobal('innerWidth', width);
	const host = document.createElement('div'); document.body.appendChild(host);
	const comment = { id: 'reply', messageId: 'message', seq: 1, from_off: 0, to_off: 4, quote: 'text', anchor_prefix: '', anchor_suffix: '', note: 'old', root_user_message_id: 'send', root_cid: 'c1.1' };
	const props = $state({ threadId: 'thread-a', comments: [comment], onChange: vi.fn() });
	const component = mount(ChatCommentSelection, { target: host, props });
	flushSync();
	await component.edit('reply');
	flushSync();
	const field = () => document.querySelector<HTMLTextAreaElement>('textarea');
	if (text !== null) { field()!.value = text; field()!.dispatchEvent(new Event('input', { bubbles: true })); }
	flushSync();
	return { host, component, props, field };
}
async function resizeTo(width: number) {
	vi.stubGlobal('innerWidth', width);
	window.dispatchEvent(new Event('resize'));
	flushSync(); await tick(); flushSync();
}

it.each([[1440, 700], [700, 1440]])('keeps typed text when the window resizes from %i to %i', async (from, to) => {
	const { host, component, field } = await openTyped(from);
	await resizeTo(to);
	expect(field()?.value).toBe('typed so far');
	await unmount(component); host.remove(); vi.unstubAllGlobals();
});

it('keeps typed text through a thread switch and return', async () => {
	const { host, component, props, field } = await openTyped(1440);
	props.threadId = 'thread-b';
	flushSync(); await tick(); flushSync();
	expect(field()).toBeNull();
	props.threadId = 'thread-a';
	flushSync(); await tick(); flushSync();
	expect(field()?.value).toBe('typed so far');
	await unmount(component); host.remove(); vi.unstubAllGlobals();
});

it('keeps typed text when an outside click closes the popover just before a thread switch', async () => {
	const { host, component, props, field } = await openTyped(1440);
	document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
	flushSync(); await tick();
	props.threadId = 'thread-b';
	flushSync(); await tick(); flushSync();
	props.threadId = 'thread-a';
	flushSync(); await tick(); flushSync();
	expect(field()?.value).toBe('typed so far');
	await unmount(component); host.remove(); vi.unstubAllGlobals();
});

async function roundTrip(props: { threadId: string }) {
	props.threadId = 'thread-b';
	flushSync(); await tick(); flushSync();
	props.threadId = 'thread-a';
	flushSync(); await tick(); flushSync();
}

it('does not reopen an edit popover nobody typed in after an outside click and a thread round trip', async () => {
	const { host, component, props, field } = await openTyped(1440, null);
	document.body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
	flushSync(); await tick();
	await roundTrip(props);
	expect(field()).toBeNull();
	await unmount(component); host.remove(); vi.unstubAllGlobals();
});

it.each(['Cancel', 'Save'])('does not reopen a popover closed with %s just before a thread switch', async (label) => {
	const { host, component, props, field } = await openTyped(1440);
	[...document.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent?.trim() === label)!.click();
	await roundTrip(props);
	expect(field()).toBeNull();
	await unmount(component); host.remove(); vi.unstubAllGlobals();
});
