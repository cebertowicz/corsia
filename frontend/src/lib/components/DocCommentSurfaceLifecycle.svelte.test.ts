import { flushSync, mount, tick, unmount } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import DocCommentSurface from './DocCommentSurface.svelte';
import type { CommentSurfaceState } from '$lib/docCommentSurfaceState.svelte';
import { createMentionSelection } from './comments/mentionSelection';

vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: vi.fn(), seedDraft: vi.fn() }));
vi.mock('$lib/stores/docComments', () => ({ docComments: { subscribe: (run: (value: object) => void) => { run({}); return () => {}; } }, loadComments: vi.fn() }));

describe('comment sheet visibility', () => {
	it('submits the selected identity after the comment surface is recreated', async () => {
		const host = document.body.appendChild(document.createElement('div'));
		const mentionSelection = createMentionSelection('😀 @Agent');
		mentionSelection.select('agent-id', 3, '@Agent');
		const onSubmit = vi.fn(async () => {});
		const props = { state: { kind: 'draft' } as CommentSurfaceState, anchor: () => new DOMRect(), narrow: false,
			comments: [], focusedId: null, draft: { text: 'quoted', body: '😀 @Agent', mentionSelection },
			onClose: vi.fn(), restoreFocus: vi.fn(), onFocus: vi.fn(), onSubmit,
			onReply: vi.fn(), onResolve: vi.fn(), onAgree: vi.fn(), target: null, artifactId: 'doc' };
		let surface = mount(DocCommentSurface, { target: host, props }); flushSync();
		await unmount(surface); surface = mount(DocCommentSurface, { target: host, props }); flushSync();
		document.body.querySelector<HTMLButtonElement>('button.submit')!.click(); await tick();
		expect(onSubmit).toHaveBeenCalledWith('😀 @Agent', ['agent-id'], [{ agent_id: 'agent-id', start: 3, end: 9 }]);
		await unmount(surface); host.remove();
	});
	it.each([false, true])('stays up through close and goes down on %s', async (unmountDuringOutro) => {
		const host = document.body.appendChild(document.createElement('div'));
		let state = $state<CommentSurfaceState>(null);
		const onChange = vi.fn();
		const restoreFocus = vi.fn();
		const props = {
			get state() { return state; }, anchor: () => new DOMRect(), narrow: true, comments: [], focusedId: null,
			draft: null, onClose: vi.fn(), restoreFocus, onFocus: vi.fn(), onSubmit: vi.fn(),
			onReply: vi.fn(), onResolve: vi.fn(), onAgree: vi.fn(), target: null, artifactId: 'doc',
			onSheetModalChange: onChange,
		};
		const component = mount(DocCommentSurface, { target: host, props });
		flushSync();
		state = { kind: 'draft' };
		flushSync();
		expect(onChange).toHaveBeenLastCalledWith(true);
		const drawer = document.querySelector('.drawer-motion');
		expect(drawer).not.toBeNull();
		state = null;
		flushSync();
		expect(onChange).toHaveBeenLastCalledWith(true);
		if (unmountDuringOutro) {
			unmount(component);
		} else {
			drawer!.dispatchEvent(new Event('outroend'));
			flushSync();
			expect(onChange).toHaveBeenLastCalledWith(false);
			await tick();
			expect(restoreFocus).toHaveBeenCalledOnce();
			unmount(component);
		}
		expect(onChange).toHaveBeenLastCalledWith(false);
		host.remove();
	});
	it('leaves focus where the person put it while the sheet was closing', async () => {
		const host = document.body.appendChild(document.createElement('div'));
		const editor = document.body.appendChild(document.createElement('textarea'));
		let state = $state<CommentSurfaceState>({ kind: 'draft' });
		const restoreFocus = vi.fn();
		const component = mount(DocCommentSurface, { target: host, props: {
			get state() { return state; }, anchor: () => new DOMRect(), narrow: true, comments: [], focusedId: null,
			draft: null, onClose: vi.fn(), restoreFocus, onFocus: vi.fn(), onSubmit: vi.fn(),
			onReply: vi.fn(), onResolve: vi.fn(), onAgree: vi.fn(), target: null, artifactId: 'doc',
		} });
		flushSync();
		const drawer = document.querySelector('.drawer-motion');
		state = null;
		flushSync();
		// A slow outro: the person is already typing in the document when it ends.
		editor.focus();
		drawer!.dispatchEvent(new Event('outroend'));
		flushSync();
		await tick();
		expect(restoreFocus).not.toHaveBeenCalled();
		expect(document.activeElement).toBe(editor);
		unmount(component);
		editor.remove(); host.remove();
	});
});
