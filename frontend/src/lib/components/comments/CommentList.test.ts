import { afterEach, expect, it, vi } from 'vitest';
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
	host.querySelector<HTMLTextAreaElement>('[data-comment-id="a"] .ct-input')!.focus(); flushSync();
	expect(ids()).toEqual(['a', 'b']);
	const outside = document.createElement('button'); host.appendChild(outside); outside.focus(); flushSync();
	expect(ids()).toEqual(['b', 'a']);
});

it('shows a card quote as plain text, for a placed comment and for one whose text changed', () => {
	host = document.createElement('div'); document.body.appendChild(host);
	const comment = (id: string, anchor: string, orphaned = false) => ({ id, anchor, orphaned, author: 'you', text: id, at: 'now', resolved: false, replies: [] });
	component = createClassComponent({ target: host, component: CommentList, props: { documentOrder: true, comments: [comment('a', '**Bravo**'), comment('b', 'See [the spec](/spec)', true)], cellLabel: (item: { id: string }) => item.id === 'a' ? '“Bravo” · Delta, Status' : null } });
	flushSync();
	const jumps = [...host.querySelectorAll('[data-comment-jump]')].map((button) => button.textContent?.trim());
	expect(jumps).toContain('“Bravo” · Delta, Status');
	expect(jumps.some((text) => text?.includes('Text changed: “See the spec”'))).toBe(true);
	expect(host.textContent).not.toContain('**');
});

it('names a comment on a diagram or table by its label in the jump button, not by its raw source', () => {
	host = document.createElement('div'); document.body.appendChild(host);
	const fence = '```mermaid\ngraph TD\n  A --> B\n```';
	const comment = (id: string, anchor: string) => ({ id, anchor, orphaned: false, author: 'you', text: id, at: 'now', resolved: false, replies: [] });
	component = createClassComponent({ target: host, component: CommentList, props: { documentOrder: true, comments: [comment('d', fence), comment('p', 'plain words')], cellLabel: (item: { id: string }) => item.id === 'd' ? 'The whole diagram' : null } });
	flushSync();
	const jumps = [...host.querySelectorAll('[data-comment-jump]')].map((button) => button.getAttribute('aria-label'));
	expect(jumps).toEqual(['Go to text: The whole diagram', 'Go to text: plain words']);
});

it("goes to a text comment's text when its card is pressed, but not from a card control or on an unplaced card", () => {
	host = document.createElement('div'); document.body.appendChild(host);
	const comment = (id: string, extra: Record<string, unknown> = {}) => ({ id, anchor: id, author: 'you', text: `body of ${id}`, at: 'now', resolved: false, replies: [], ...extra });
	const onFocus = vi.fn();
	component = createClassComponent({ target: host, component: CommentList, props: { documentOrder: true, onFocus, comments: [comment('a'), comment('o', { orphaned: true }), comment('u', { drawn: false, placement: 'not_placed' }), comment('r', { regionNumber: 1 })] } });
	flushSync();
	const card = (id: string) => host.querySelector<HTMLElement>(`[data-comment-id="${id}"]`)!;
	card('a').querySelector<HTMLElement>('.ct')!.click();
	expect(onFocus).toHaveBeenCalledTimes(1);
	expect(onFocus).toHaveBeenLastCalledWith('a');
	card('o').querySelector<HTMLElement>('.ct')!.click();
	expect(onFocus).toHaveBeenCalledTimes(2);
	expect(onFocus).toHaveBeenLastCalledWith('o');
	card('r').querySelector<HTMLElement>('.ct')!.click();
	expect(onFocus).toHaveBeenCalledTimes(3);
	expect(onFocus).toHaveBeenLastCalledWith('r');
	onFocus.mockClear();
	card('a').querySelector<HTMLButtonElement>('.ct-act.resolve')!.click();
	card('u').querySelector<HTMLElement>('.ct')!.click();
	expect(onFocus).not.toHaveBeenCalled();
});
