import { afterEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { createCommentSurfaceState } from './docCommentSurfaceState.svelte';

afterEach(() => focusedDocComment.set(null));

describe('comment surface state', () => {
	it('opens a comment when the caret lands in its text', () => {
		const surface = createCommentSurfaceState(() => {});
		surface.focusComment('c1', new DOMRect());
		expect(surface.state).toEqual({ kind: 'thread', id: 'c1' });
		expect(get(focusedDocComment)).toBe('c1');
	});

	it('keeps the comments drawer open when the caret lands in commented text', () => {
		const surface = createCommentSurfaceState(() => {});
		surface.openList(document.createElement('button'));
		expect(surface.focusComment('c1', new DOMRect())).toBe(false);
		expect(surface.state).toEqual({ kind: 'list' });
		expect(get(focusedDocComment)).toBeNull();
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
		expect(surface.state).toEqual({ kind: 'thread', id: 's1' });
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

	it('tracks the hovered suggestion without moving focus', () => {
		const surface = createCommentSurfaceState(() => {});
		surface.hover('s1');
		expect(surface.hovered).toBe('s1');
		expect(get(focusedDocComment)).toBeNull();
		expect(surface.state).toBeNull();
		surface.hover(null);
		expect(surface.hovered).toBeNull();
	});
});
