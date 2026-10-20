import { get } from 'svelte/store';
import { afterEach, describe, expect, it } from 'vitest';
import { canvasCommentHighlights, commentsInDocumentOrder } from '$lib/docCanvasComments';
import { docComments, type DocComment } from './docComments';
import { focusedDocComment } from './docCommentFocus';

const TEXT = 'first second';
const row = (id: string, from: number, anchor: string, overrides: Partial<DocComment> = {}): DocComment => ({
	id, from, to: from + anchor.length, anchor, author: 'you', text: id, at: 'now', resolved: false, replies: [], ...overrides,
});
const rows = () => get(docComments).doc ?? [];
const set = (next: DocComment[]) => docComments.set({ doc: next });
const resolve = (id: string, resolved: boolean) => docComments.update((all) => ({ doc: all.doc.map((c) => c.id === id ? { ...c, resolved } : c) }));
const highlighted = () => canvasCommentHighlights(commentsInDocumentOrder(rows(), TEXT), TEXT, get(focusedDocComment), undefined).map((range) => range.id);

afterEach(() => { docComments.set({}); focusedDocComment.set(null); });

describe('a resolved comment is highlighted only while focused', () => {
	it('loses its highlight when the focused comment is resolved, and gets it back as an open comment on re-open', () => {
		set([row('a', 0, 'first'), row('b', 6, 'second')]);
		focusedDocComment.set('a');
		expect(highlighted()).toEqual(['a', 'b']);
		resolve('a', true);
		expect(get(focusedDocComment)).toBeNull();
		expect(highlighted()).toEqual(['b']);
		resolve('a', false);
		expect(highlighted()).toEqual(['a', 'b']);
	});

	it('loses it when the resolve arrives from elsewhere, as a fresh load of the same comments', () => {
		set([row('a', 0, 'first'), row('b', 6, 'second')]);
		focusedDocComment.set('a');
		set([row('a', 0, 'first', { resolved: true }), row('b', 6, 'second')]);
		expect(get(focusedDocComment)).toBeNull();
		expect(highlighted()).toEqual(['b']);
	});

	it('shows a resolved comment the person picks in the drawer, and keeps the focus of an unrelated one', () => {
		set([row('a', 0, 'first', { resolved: true }), row('b', 6, 'second')]);
		expect(highlighted()).toEqual(['b']);
		focusedDocComment.set('a');
		expect(highlighted()).toEqual(['a', 'b']);
		resolve('b', true);
		expect(get(focusedDocComment)).toBe('a');
		expect(highlighted()).toEqual(['a']);
	});
});
