import { ViewPlugin, type EditorView, type ViewUpdate } from '@codemirror/view';
import { commentRanges } from './comment-marks';
import { placeHighlightDot, type NewSignName } from '$lib/components/comments/highlightDot';

/**
 * The new sign on each unread thread's highlight, as the chat draws it: pinned over the end of the highlight, out of
 * the text's flow, and named 'New reply' when a reply is unread, else 'New comment'. A highlight is found where it is drawn: the editor's own mark, the painted text of a table cell or
 * diagram, or the sign of an image comment or of a thread whose text was deleted (`image-pins.ts`). Signs are placed again whenever the editor redraws its lines.
 */
class NewSigns {
	private placed: { anchor: Element; name: NewSignName; cleanup: () => void }[] = [];
	/** An image comment's sign is drawn in the same measure as this one is read, so a highlight not found yet is looked for once more. */
	private retried = false;

	constructor(private readonly view: EditorView) { this.schedule(); }

	update(update: ViewUpdate) {
		const ranges = update.startState.field(commentRanges) !== update.state.field(commentRanges);
		if (ranges || update.docChanged || update.viewportChanged || update.geometryChanged) { this.retried = false; this.schedule(); }
	}

	destroy() { this.clear(); }

	private schedule() {
		this.view.requestMeasure({ key: this, read: (view) => this.anchors(view), write: (anchors) => this.place(anchors) });
	}

	private anchors(view: EditorView): { anchors: { anchor: HTMLElement; name: NewSignName }[]; missing: boolean } {
		let missing = false;
		const anchors = view.state.field(commentRanges).flatMap((range) => {
			if (!range.unread) return [];
			const id = CSS.escape(range.id);
			const mark = [...view.contentDOM.querySelectorAll<HTMLElement>(`[data-comment-new="${id}"]`)].at(-1)
				?? [...view.contentDOM.querySelectorAll<HTMLElement>('[data-comment-ids]')].filter((el) => el.dataset.commentIds?.split(',').includes(range.id)).at(-1)
				?? view.scrollDOM.querySelector<HTMLElement>(`.cm-image-pin[data-comment-id="${id}"]`);
			if (!mark) missing = true;
			return mark ? [{ anchor: mark, name: range.unread === 'reply' ? 'New reply' as const : 'New comment' as const }] : [];
		});
		return { anchors, missing };
	}

	private place({ anchors, missing }: { anchors: { anchor: HTMLElement; name: NewSignName }[]; missing: boolean }) {
		if (missing && !this.retried) { this.retried = true; requestAnimationFrame(() => this.schedule()); }
		if (anchors.length === this.placed.length && anchors.every(({ anchor, name }, index) => anchor === this.placed[index].anchor && name === this.placed[index].name)) return;
		this.clear();
		this.placed = anchors.map(({ anchor, name }) => ({ anchor, name, cleanup: placeHighlightDot(this.view.contentDOM, anchor, null, undefined, name) }));
	}

	private clear() {
		for (const { cleanup } of this.placed) cleanup();
		this.placed = [];
	}
}

export const commentNewSigns = [commentRanges, ViewPlugin.fromClass(NewSigns)];
