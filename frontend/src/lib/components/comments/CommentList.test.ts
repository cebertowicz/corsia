import { afterEach, expect, it } from 'vitest';
import { flushSync } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import CommentList from './CommentList.svelte';

let host: HTMLElement;
let component: ReturnType<typeof createClassComponent> | null = null;
afterEach(() => { component?.$destroy(); component = null; host?.remove(); });
it('holds document order through focus changes inside a card and updates after leaving it', () => {
	host = document.createElement('div'); document.body.appendChild(host);
	const comment = (id: string) => ({ id, anchor: id, author: 'you', text: id, at: 'now', resolved: false, replies: [] });
	const a = comment('a'), b = comment('b');
	component = createClassComponent({ target: host, component: CommentList, props: { comments: [a, b], documentOrder: true } });
	flushSync();
	const ids = () => [...host.querySelectorAll('[data-comment-id]')].map((card) => card.getAttribute('data-comment-id'));
	host.querySelector<HTMLButtonElement>('[data-comment-jump]')!.focus();
	flushSync();
	component.$set({ comments: [b, a] }); flushSync();
	expect(ids()).toEqual(['a', 'b']);
	host.querySelector<HTMLInputElement>('[data-comment-id="a"] .ct-input')!.focus(); flushSync();
	expect(ids()).toEqual(['a', 'b']);
	const outside = document.createElement('button'); host.appendChild(outside); outside.focus(); flushSync();
	expect(ids()).toEqual(['b', 'a']);
});
