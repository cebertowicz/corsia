import { flushSync, mount, tick, unmount } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import DocCommentSurface from './DocCommentSurface.svelte';
import type { CommentSurfaceState } from '$lib/docCommentSurfaceState.svelte';

vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: vi.fn(), seedDraft: vi.fn() }));
vi.mock('$lib/stores/docComments', () => ({ loadComments: vi.fn() }));

describe('comment sheet visibility', () => {
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
});
