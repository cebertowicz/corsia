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
