import { flushSync } from 'svelte';
import { describe, expect, it } from 'vitest';
import type { DocComment } from '$lib/stores/docComments';
import { createCommentScrollTarget } from './docViewerScrollTarget.svelte';

const row = (id: string, from: number, anchor: string, extra: Partial<DocComment> = {}): DocComment => ({
	id, from, to: from + anchor.length, anchor, author: 'you', text: id, at: 'now', resolved: false, replies: [], ...extra,
});

function setup() {
	const text = 'alpha beta ![image](image.jpg)';
	let focused = $state<string | null>(null);
	let comments = $state.raw([
		row('text', 0, 'alpha'), row('margin', 6, 'beta'),
		row('image', 11, '![image](image.jpg)', { region: { x: 0.3, y: 0.4, w: 0, h: 0 }, imageSrc: 'image.jpg' }),
	]);
	let scroll!: ReturnType<typeof createCommentScrollTarget>;
	const stop = $effect.root(() => { scroll = createCommentScrollTarget({ focused: () => focused, comments: () => comments, text: () => text, listOpen: () => false }); });
	flushSync();
	return {
		scroll, stop,
		focus(id: string) { focused = id; flushSync(); },
		echo() { comments = comments.map(comment => ({ ...comment, drawn: true })); flushSync(); },
	};
}

describe('comment focus across the document and image viewer', () => {
	it('does not navigate the covered document when an image post, echo or tag focuses its region', () => {
		const test = setup();
		try {
			test.focus('image');
			expect(test.scroll.value).toBeNull();
			test.echo();
			expect(test.scroll.value).toBeNull();
		} finally { test.stop(); }
	});

	it('preserves the save and margin targets through global text focus and comment echo', () => {
		const test = setup();
		try {
			test.scroll.toComment('text', true, 'comment-save');
			const saved = test.scroll.value;
			test.focus('text'); test.echo();
			expect(test.scroll.value).toBe(saved);
			expect(saved?.origin).toBe('comment-save');
			test.scroll.toComment('margin', false, 'navigation');
			const margin = test.scroll.value;
			test.focus('margin'); test.echo();
			expect(test.scroll.value).toBe(margin);
			expect(margin).toMatchObject({ from: 6, to: 10, origin: 'navigation' });
		} finally { test.stop(); }
	});

	it('still navigates text when focus arrives without an explicit target', () => {
		const test = setup();
		try {
			test.focus('text');
			expect(test.scroll.value).toMatchObject({ from: 0, to: 5, origin: 'navigation' });
		} finally { test.stop(); }
	});
});
