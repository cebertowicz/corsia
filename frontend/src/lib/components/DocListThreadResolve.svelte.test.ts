import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { fromStore, writable } from 'svelte/store';
import DocCommentSurface from './DocCommentSurface.svelte';
import { createMentionSelection } from './comments/mentionSelection';
import { keptCommentDrafts, replyDraftKey } from '$lib/stores/keptCommentDrafts.svelte';

vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: vi.fn(), seedDraft: vi.fn() }));

let component: Record<string, unknown> | undefined;
let host: HTMLElement;
afterEach(() => { if (component) unmount(component); host?.remove(); keptCommentDrafts.reset(); });

it.each(['resolve', 'draft', 'agree'] as const)('handles %s in a phone thread opened from the list', async (action) => {
	const comment = { id: 'a', anchor: 'text', author: 'you', text: 'A comment', at: 'now', resolved: false,
		replies: Array.from({ length: 6 }, (_, i) => ({ id: `r${i}`, author: 'you', text: `Reply ${i}`, at: 'now' })) };
	const comments = writable([comment]), live = fromStore(comments);
	const onClose = vi.fn();
	host = document.body.appendChild(document.createElement('div'));
	component = mount(DocCommentSurface, { target: host, props: {
		state: { kind: 'list' }, anchor: null, narrow: true, phone: true,
		get comments() { return live.current; }, focusedId: null, draft: null, draftScope: 'doc',
		onClose, restoreFocus: vi.fn(), onFocus: vi.fn(), onSubmit: vi.fn(), onReply: vi.fn(),
		onResolve: vi.fn(async () => { comments.set([{ ...comment, resolved: true }]); return true; }), onAgree: vi.fn(), target: null, artifactId: 'doc',
	} });
	flushSync();
	const drawer = document.body.querySelector<HTMLElement>('[data-doc-comments-drawer]')!;
	drawer.querySelector<HTMLButtonElement>('.ct-open-thread')!.click(); flushSync(); await tick();
	expect(drawer.querySelector('[data-drawer-back]')).not.toBeNull();
	if (action === 'draft') { keptCommentDrafts.keepReply(replyDraftKey('doc', 'a'), 'Unsent reply', createMentionSelection()); flushSync(); }
	const button = drawer.querySelector<HTMLButtonElement>(`button[aria-label="${action === 'agree' ? 'Agree with this comment' : 'Resolve'}"]`)!;
	button.click(); flushSync(); await tick(); flushSync(); await tick();
	expect(onClose).not.toHaveBeenCalled();
	expect(drawer.querySelector('[data-drawer-back]') === null).toBe(action === 'resolve');
	if (action === 'draft') expect(keptCommentDrafts.get(replyDraftKey('doc', 'a'))?.body).toBe('Unsent reply');
});

it('preserves highlight-sheet Resolve after a list thread was opened', async () => {
	const comment = { id: 'a', anchor: 'text', author: 'you', text: 'A comment', at: 'now', resolved: false,
		replies: Array.from({ length: 6 }, (_, i) => ({ id: `r${i}`, author: 'you', text: `Reply ${i}`, at: 'now' })) };
	const comments = writable([comment]), live = fromStore(comments);
	const state = writable<{ kind: 'list' } | { kind: 'thread'; id: string }>({ kind: 'list' }), liveState = fromStore(state);
	const onClose = vi.fn();
	host = document.body.appendChild(document.createElement('div'));
	component = mount(DocCommentSurface, { target: host, props: {
		get state() { return liveState.current; }, anchor: null, narrow: true, phone: true,
		get comments() { return live.current; }, focusedId: null, draft: null,
		onClose, restoreFocus: vi.fn(), onFocus: vi.fn(), onSubmit: vi.fn(), onReply: vi.fn(),
		onResolve: vi.fn(async () => { comments.set([{ ...comment, resolved: true }]); return true; }), onAgree: vi.fn(), target: null, artifactId: 'doc',
	} });
	flushSync();
	document.body.querySelector<HTMLButtonElement>('.ct-open-thread')!.click(); flushSync(); await tick();
	expect(document.body.querySelector('[data-drawer-back]')).not.toBeNull();
	state.set({ kind: 'thread', id: 'a' }); flushSync(); await tick();
	const resolve = [...document.body.querySelectorAll<HTMLButtonElement>('.sheet-act')].find((button) => button.textContent?.trim() === 'Resolve');
	expect(resolve).toBeDefined();
	resolve!.click(); flushSync(); await tick(); flushSync(); await tick();
	expect(onClose).toHaveBeenCalledOnce();
});
