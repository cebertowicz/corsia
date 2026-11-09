import { flushSync, mount, tick, unmount } from 'svelte';
import { fromStore, get } from 'svelte/store';
import { afterEach, expect, it, vi } from 'vitest';
import { createCommentSurfaceState } from '$lib/docCommentSurfaceState.svelte';
import { docComments, type DocComment } from '$lib/stores/docComments';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { ANCHOR_MOVED } from './ui/Popover.svelte';
import DocCommentSurface from './DocCommentSurface.svelte';

vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: vi.fn(), seedDraft: vi.fn() }));

afterEach(() => {
	docComments.set({});
	focusedDocComment.set(null);
	vi.unstubAllGlobals();
});

it('keeps a resolved card at its last anchor after shared focus clears', async () => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	const scrollIntoView = HTMLElement.prototype.scrollIntoView;
	HTMLElement.prototype.scrollIntoView = vi.fn();
	const host = document.body.appendChild(document.createElement('div'));
	const bounds = document.body.appendChild(document.createElement('div'));
	bounds.getBoundingClientRect = () => new DOMRect(0, 0, 600, 600);
	const comment: DocComment = { id: 'a', from: 0, to: 4, anchor: 'text', author: 'you', text: 'A comment', at: 'now', resolved: false, replies: [] };
	docComments.set({ doc: [comment] });
	const comments = fromStore(docComments);
	const ui = createCommentSurfaceState(() => {});
	let rect: DOMRect | null = new DOMRect(100, 120, 40, 20);
	const anchor = vi.fn(() => rect);
	ui.focusComment('a', anchor);
	const onClose = vi.fn();
	const component = mount(DocCommentSurface, { target: host, props: {
		get state() { return ui.state; }, get anchor() { return ui.anchor; }, bounds, narrow: false,
		get comments() { return comments.current.doc ?? []; }, get focusedId() { return get(focusedDocComment); },
		draft: null, onClose, restoreFocus: vi.fn(), onFocus: vi.fn(), onSubmit: vi.fn(), onReply: vi.fn(),
		onResolve: vi.fn(), onAgree: vi.fn(), target: null, artifactId: 'doc',
	} });
	flushSync();
	await vi.waitFor(() => expect(document.querySelector('[data-popover]')?.getAttribute('aria-hidden')).toBeNull());
	const popover = document.querySelector<HTMLElement>('[data-popover]')!;
	const position = popover.style.cssText;
	expect(anchor).toHaveBeenCalled();
	rect = null;
	docComments.set({ doc: [{ ...comment, resolved: true }] });
	flushSync();
	await tick();
	window.dispatchEvent(new Event(ANCHOR_MOVED));
	flushSync();
	await tick();
	expect(get(focusedDocComment)).toBeNull();
	expect(ui.state).toEqual({ kind: 'thread', id: 'a', fromText: true });
	expect(onClose).not.toHaveBeenCalled();
	expect(popover.getAttribute('aria-hidden')).toBeNull();
	expect(popover.style.cssText).toBe(position);
	await unmount(component);
	HTMLElement.prototype.scrollIntoView = scrollIntoView;
	host.remove();
	bounds.remove();
});
