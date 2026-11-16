import { flushSync } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it } from 'vitest';
import { createCommentSurfaceState } from './docCommentSurfaceState.svelte';
import { createHeldUnread, openCommentIds } from './docHeldUnread.svelte';
import { focusedDocComment } from './stores/docCommentFocus';
import { pickedComments } from './stores/docComments';

type Listed = { id: string; newComment?: boolean; newReplyIds?: string[] };

/** The document's real surface state with its margin column up, and the held set fed the way DocViewer feeds it. */
function setup(initial: Listed[]) {
	let comments = $state<Listed[]>(initial);
	let focused = $state<string | null>(get(focusedDocComment));
	const unsubscribe = focusedDocComment.subscribe((id) => { focused = id; });
	const surface = createCommentSurfaceState(() => {}, undefined, () => true);
	let held!: ReturnType<typeof createHeldUnread>;
	const teardown = $effect.root(() => {
		held = createHeldUnread(() => openCommentIds(surface.state, focused), () => comments);
	});
	surface.openOnLoad();
	flushSync();
	return {
		surface,
		/** The highlight as drawn: the live unread state, or the held one. */
		tint: (id: string) => {
			const live = comments.find((comment) => comment.id === id);
			const unread = live?.newReplyIds?.length ? 'reply' as const : live?.newComment ? 'comment' as const : undefined;
			return held.paint({ id, unread }).unread;
		},
		read: (id: string) => { comments = comments.map((comment) => comment.id === id ? { ...comment, newComment: false, newReplyIds: [] } : comment); flushSync(); },
		pick: (id: string | null) => { focusedDocComment.set(id); flushSync(); },
		done: () => { teardown(); unsubscribe(); },
	};
}

const unreadReply = (id: string): Listed => ({ id, newReplyIds: [`${id}-a2`] });

afterEach(() => { focusedDocComment.set(null); pickedComments.set(new Set()); });

describe('the held unread tint on a document highlight', () => {
	it('holds a thread whose card is open while it is read, and lets it go when the card is released', () => {
		const t = setup([unreadReply('alpha'), { id: 'beta' }]);
		t.pick('alpha');
		t.read('alpha');
		expect(t.tint('alpha'), 'the card is open: the sign the reader came to see stays').toBe('reply');
		t.pick(null);
		expect(t.tint('alpha'), 'the card was let go with the column still up').toBeUndefined();
		t.done();
	});

	it('holds nothing for a thread read elsewhere whose card was never opened, though the column is up', () => {
		const t = setup([unreadReply('alpha')]);
		expect(t.surface.state).toEqual({ kind: 'list' });
		t.read('alpha');
		expect(t.tint('alpha')).toBeUndefined();
		t.done();
	});

	it('moves the hold from one card to the next as the reader opens them in turn', () => {
		const t = setup([unreadReply('alpha'), { id: 'beta', newComment: true }]);
		t.pick('alpha');
		t.read('alpha');
		t.pick('beta');
		t.read('beta');
		expect(t.tint('alpha'), 'alpha is no longer open').toBeUndefined();
		expect(t.tint('beta')).toBe('comment');
		t.pick(null);
		expect(t.tint('beta')).toBeUndefined();
		t.done();
	});

	it('lets go of a thread opened from its text when Escape releases the card', () => {
		const t = setup([unreadReply('alpha')]);
		t.surface.focusComment('alpha', new DOMRect(), false, false, true);
		flushSync();
		// With the column up the card is picked in place: the column stays and the picked card is the open one.
		expect(t.surface.state).toEqual({ kind: 'list' });
		expect(get(focusedDocComment)).toBe('alpha');
		t.read('alpha');
		expect(t.tint('alpha')).toBe('reply');
		// Escape in the margin column calls the surface's release, the same as DocCommentSurface's escapeColumn.
		expect(t.surface.release()).toBe(true);
		flushSync();
		expect(t.surface.state).toEqual({ kind: 'list' });
		expect(t.tint('alpha')).toBeUndefined();
		t.done();
	});

	it('does not tint a thread that was already read when its card opened', () => {
		const t = setup([{ id: 'alpha' }]);
		t.pick('alpha');
		expect(t.tint('alpha')).toBeUndefined();
		t.done();
	});
});
