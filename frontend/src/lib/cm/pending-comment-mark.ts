import { StateEffect, RangeSet } from '@codemirror/state';
import { Decoration, ViewPlugin, type DecorationSet, type ViewUpdate } from '@codemirror/view';
import { selectionRange, type CommentSelection } from '$lib/stores/commentSelection';

/** The draft's captured selection while its composer is open; null once it posts or is cancelled. */
export const setPendingComment = StateEffect.define<CommentSelection | null>();

const pending = Decoration.mark({ class: 'cm-comment-pending' });

/**
 * Draws the open composer's captured range, because focus in the composer collapses the editor's own
 * selection. It has its own class, never `cm-comment-anchor`: an unposted draft is not a placed comment. The range is never tracked here: each document change re-reads it
 * from the draft's `CommentSelection`, so the highlight follows remote and local edits exactly as the
 * capture does, and disappears when the range is lost. Register it after the document binding, which
 * writes a local edit into the shared text during its own update, before this plugin reads it.
 */
export const pendingCommentMark = ViewPlugin.fromClass(class {
	selection: CommentSelection | null = null;
	decorations: DecorationSet = Decoration.none;
	update(update: ViewUpdate) {
		let changed = update.docChanged;
		for (const transaction of update.transactions) {
			for (const effect of transaction.effects) {
				if (effect.is(setPendingComment)) { this.selection = effect.value; changed = true; }
			}
		}
		if (!changed) return;
		const range = this.selection && selectionRange(this.selection);
		const length = update.state.doc.length;
		this.decorations = range && range.to <= length ? RangeSet.of([pending.range(range.from, range.to)]) : Decoration.none;
	}
}, { decorations: (plugin) => plugin.decorations });
