import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { createCommentSurfaceState } from './docCommentSurfaceState.svelte';

afterEach(() => focusedDocComment.set(null));

describe('comment surface state', () => {
	it('opens a comment when the caret lands in its text', () => {
		const surface = createCommentSurfaceState(() => {});
		surface.focusComment('c1', new DOMRect());
		// Picked in the text: its card leaves the focus there.
		expect(surface.state).toEqual({ kind: 'thread', id: 'c1', fromText: true });
		expect(get(focusedDocComment)).toBe('c1');
	});

	it('keeps the comments drawer open when the caret lands in commented text', () => {
		const surface = createCommentSurfaceState(() => {});
		surface.openList(document.createElement('button'));
		expect(surface.focusComment('c1', new DOMRect())).toBe(false);
		expect(surface.state).toEqual({ kind: 'list' });
		expect(get(focusedDocComment)).toBeNull();
	});

	it('opens only the picked thread in place of the list when the canvas has no room for the column', () => {
		const surface = createCommentSurfaceState(() => {}, undefined, () => false);
		surface.openList(document.createElement('button'));
		expect(surface.focusComment('c1', new DOMRect())).toBe(true);
		expect(surface.state).toEqual({ kind: 'thread', id: 'c1', fromText: true });
		expect(get(focusedDocComment)).toBe('c1');
	});

	it('keeps the margin column under a new comment and goes back to it when the draft closes', () => {
		const surface = createCommentSurfaceState(() => {}, undefined, () => true);
		surface.openOnLoad();
		surface.openDraft(new DOMRect());
		expect(surface.state).toEqual({ kind: 'draft' });
		expect(surface.draftOverColumn).toBe(true);
		// A second selection while drafting is still over the column.
		surface.openDraft(new DOMRect());
		expect(surface.draftOverColumn).toBe(true);
		surface.close();
		expect(surface.state).toEqual({ kind: 'list' });
		expect(surface.draftOverColumn).toBe(false);
		surface.close();
		expect(surface.state).toBeNull();
	});

	it('closes a draft with nothing behind it, or everything when another document opens', () => {
		const surface = createCommentSurfaceState(() => {}, undefined, () => true);
		surface.openDraft(new DOMRect());
		expect(surface.draftOverColumn).toBe(false);
		surface.close();
		expect(surface.state).toBeNull();
		surface.openOnLoad();
		surface.openDraft(new DOMRect());
		surface.closeAll();
		expect(surface.state).toBeNull();
		// Without room for the column, a draft from the list is the popover's alone.
		const narrow = createCommentSurfaceState(() => {}, undefined, () => false);
		narrow.openList(document.createElement('button'));
		narrow.openDraft(new DOMRect());
		expect(narrow.draftOverColumn).toBe(false);
	});

	it('does not authorize a navigation target for an already focused caret', () => {
		const surface = createCommentSurfaceState(() => {});
		expect(surface.focusComment('c1', new DOMRect())).toBe(true);

		expect(surface.focusComment('c1', new DOMRect())).toBe(false);
		expect(get(focusedDocComment)).toBe('c1');
	});

	it('focuses the card of a suggestion pressed in the editor while the drawer stays open', () => {
		const surface = createCommentSurfaceState(() => {});
		surface.openList(document.createElement('button'));
		expect(surface.focusComment('s1', new DOMRect(), true)).toBe(true);
		expect(surface.state).toEqual({ kind: 'list' });
		expect(get(focusedDocComment)).toBe('s1');
		expect(surface.focusComment('s1', new DOMRect(), true)).toBe(false);
	});

	it('opens the card of a suggestion pressed in the editor when no drawer is open', () => {
		const surface = createCommentSurfaceState(() => {});
		expect(surface.focusComment('s1', new DOMRect(), true)).toBe(true);
		expect(surface.state).toEqual({ kind: 'thread', id: 's1', fromText: true });
	});

	it('opens one or several marker threads and keeps the next when the focused thread is deleted', () => {
		const surface = createCommentSurfaceState(() => {});
		const opener = document.createElement('button');
		surface.focusMarker(['a'], opener);
		expect(surface.state).toEqual({ kind: 'thread', id: 'a' });
		expect(get(focusedDocComment)).toBe('a');
		surface.focusMarker(['a', 'b', 'c'], opener);
		expect(surface.state).toEqual({ kind: 'thread', id: 'a', ids: ['a', 'b', 'c'] });
		surface.focusRemainingThread(['b', 'c']);
		expect(surface.state).toEqual({ kind: 'thread', id: 'b', ids: ['a', 'b', 'c'] });
		expect(get(focusedDocComment)).toBe('b');
		surface.focusComment('c', new DOMRect());
		expect(surface.state).toEqual({ kind: 'thread', id: 'c', ids: ['a', 'b', 'c'] });
		surface.focusRemainingThread(['a', 'b']);
		expect(surface.state).toEqual({ kind: 'thread', id: 'a', ids: ['a', 'b', 'c'] });
		surface.focusRemainingThread([]);
		expect(surface.state).toBeNull();
	});

	it('opens a card at the click only when a click opened it', () => {
		const surface = createCommentSurfaceState(() => {});
		const opener = document.createElement('button');
		surface.focusComment('c1', new DOMRect(), false, true);
		expect(surface.atClick).toBe(true);
		surface.focusComment('c2', new DOMRect());
		expect(surface.atClick).toBe(false);
		surface.focusMarker(['a'], opener, undefined, true);
		expect(surface.atClick).toBe(true);
		surface.openList(opener);
		expect(surface.atClick).toBe(false);
		surface.focusMarker(['a'], opener, undefined, true);
		surface.openDraft(new DOMRect());
		expect(surface.atClick).toBe(false);
	});

	it('tracks the hovered suggestion without moving focus', () => {
		const surface = createCommentSurfaceState(() => {});
		surface.hover('s1');
		expect(surface.hovered).toBe('s1');
		expect(get(focusedDocComment)).toBeNull();
		expect(surface.state).toBeNull();
		surface.hover(null);
		expect(surface.hovered).toBeNull();
	});

	describe('release: a picked margin card lets go', () => {
		it('returns a card picked from rest to the column and clears the highlight, without pulling focus', () => {
			const surface = createCommentSurfaceState(() => {}, undefined, () => true);
			surface.focusComment('c1', new DOMRect(), false, true);
			expect(surface.state).toEqual({ kind: 'thread', id: 'c1', fromText: true });
			expect(surface.release()).toBe(true);
			expect(surface.state).toEqual({ kind: 'list' });
			expect(surface.quiet).toBe(true);
			expect(get(focusedDocComment)).toBeNull();
		});

		it('clears the pick when the column is already showing (a pick leaves the list state with only the focus set)', () => {
			const surface = createCommentSurfaceState(() => {}, undefined, () => true);
			surface.openList(document.createElement('button'));
			surface.focusComment('c1', new DOMRect());
			expect(get(focusedDocComment)).toBe('c1');
			expect(surface.release()).toBe(true);
			expect(surface.state).toEqual({ kind: 'list' });
			expect(get(focusedDocComment)).toBeNull();
		});

		it('closes the column to the editor after a pick that a caret in plain text released', () => {
			const editor = vi.fn();
			const surface = createCommentSurfaceState(editor, undefined, () => true);
			surface.focusComment('c1', new DOMRect(), false, true);
			expect(surface.release()).toBe(true);
			surface.close();
			surface.restoreFocus();
			expect(surface.state).toBeNull();
			expect(editor).toHaveBeenCalledTimes(1);
		});

		it('closes the column to the button that opened it after a draft over it was cancelled', () => {
			const editor = vi.fn(), button = document.body.appendChild(document.createElement('button'));
			const surface = createCommentSurfaceState(editor, undefined, () => true);
			surface.openList(button);
			surface.openDraft(new DOMRect());
			surface.close();
			expect(surface.state).toEqual({ kind: 'list' });
			surface.close();
			surface.restoreFocus();
			expect(document.activeElement).toBe(button);
			expect(editor).not.toHaveBeenCalled();
			button.remove();
		});

		it('does nothing when nothing is picked, or without the margin column', () => {
			const margin = createCommentSurfaceState(() => {}, undefined, () => true);
			margin.openList(document.createElement('button'));
			expect(margin.release()).toBe(false);
			const popover = createCommentSurfaceState(() => {});
			popover.focusComment('c1', new DOMRect());
			expect(popover.release()).toBe(false);
			expect(popover.state).toEqual({ kind: 'thread', id: 'c1', fromText: true });
			expect(get(focusedDocComment)).toBe('c1');
		});
	});
});
