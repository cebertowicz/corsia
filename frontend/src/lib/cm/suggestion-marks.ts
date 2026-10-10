import type { EditorState, Extension, Range } from '@codemirror/state';
import { Decoration, EditorView, WidgetType, type DecorationSet } from '@codemirror/view';
import { commentRanges, type InlineSuggestion } from './comment-marks';
import { suggestionTones } from './suggestion-tones';

const ID_ATTR = 'data-suggestion-id';
const HEX = /^#[0-9a-f]{6}$/i;

/**
 * Typed-reveal seam: receives the full insert and returns the text the widget shows. It returns the whole
 * insert today; the reveal replaces this body once it can drive a progressive value.
 */
export function suggestionInsertShown(insert: string): string {
	return insert;
}

/** The suggestion's own colour as style variables: raw for the tint, plus a text tone per theme (4.5:1). */
function colourStyle(colour: string): string {
	const base = HEX.test(colour) ? colour : 'var(--accent)';
	const tones = suggestionTones(base);
	return `--suggestion-color:${base};--suggestion-tone-light:${tones.light};--suggestion-tone-dark:${tones.dark}`;
}

/**
 * The inserted text. It is not document text: the string sits in an attribute and is drawn by CSS, so it is
 * neither selectable nor copied, and in-page find never matches it.
 */
class SuggestionInsert extends WidgetType {
	constructor(
		private readonly id: string,
		private readonly suggestion: InlineSuggestion,
		private readonly focused: boolean,
		private readonly afterStrike: boolean,
	) { super(); }

	eq(other: SuggestionInsert): boolean {
		return this.id === other.id && this.focused === other.focused && this.afterStrike === other.afterStrike
			&& this.suggestion.insert === other.suggestion.insert && this.suggestion.color === other.suggestion.color
			&& !!this.suggestion.busy === !!other.suggestion.busy;
	}

	toDOM(): HTMLElement {
		const insert = document.createElement('span');
		insert.className = 'cm-suggestion-insert';
		if (this.focused) insert.classList.add('cm-suggestion-focus');
		if (this.afterStrike) insert.classList.add('cm-suggestion-insert-after');
		insert.setAttribute(ID_ATTR, this.id);
		insert.setAttribute('style', colourStyle(this.suggestion.color));
		insert.setAttribute('data-insert', suggestionInsertShown(this.suggestion.insert));
		insert.contentEditable = 'false';
		// Screen readers read CSS generated content, so the drawn insert would run on from the struck text with no cue.
		// Its card carries the suggestion for assistive tech instead.
		insert.setAttribute('aria-hidden', 'true');
		if (this.suggestion.busy) insert.setAttribute('aria-busy', 'true');
		return insert;
	}

	// Pointer events reach the editor's handlers below, which focus the card.
	ignoreEvent(): boolean { return false; }
}

function marks(state: EditorState): DecorationSet {
	const length = state.doc.length;
	const out: Range<Decoration>[] = [];
	for (const range of state.field(commentRanges)) {
		const suggestion = range.suggestion;
		if (!suggestion) continue;
		const from = Math.min(range.from, length), to = Math.min(range.to, length);
		const style = colourStyle(suggestion.color);
		const focus = range.focused ? ' cm-suggestion-focus' : '';
		// Overlapped by an earlier open suggestion: its card shows it, and the editor only tints its range while focused.
		if (!suggestion.inline) {
			if (focus && from < to) out.push(Decoration.mark({ class: 'cm-suggestion-focus', attributes: { style } }).range(from, to));
			continue;
		}
		if (from < to) {
			const attributes: Record<string, string> = { style, [ID_ATTR]: range.id };
			if (suggestion.busy) attributes['aria-busy'] = 'true';
			out.push(Decoration.mark({ class: `cm-suggestion-del${focus}`, attributes }).range(from, to));
		}
		if (suggestion.insert) {
			out.push(Decoration.widget({
				widget: new SuggestionInsert(range.id, suggestion, !!range.focused, from < to),
				side: 1,
			}).range(to));
		}
	}
	return Decoration.set(out, true);
}

export interface SuggestionPointer {
	/** The insert was pressed (or tapped): focus its card. */
	focus: (id: string, anchor: DOMRect) => void;
	/** The pointer entered a suggestion's strike or insert, or left it (null). */
	hover: (id: string | null) => void;
}

const suggestionOf = (target: EventTarget | null): HTMLElement | null =>
	target instanceof Element ? target.closest<HTMLElement>(`[${ID_ATTR}]`) : null;

const theme = EditorView.baseTheme({
	'.cm-suggestion-del': {
		color: 'var(--suggestion-tone-dark)',
		textDecoration: 'line-through',
		textDecorationThickness: '1px',
	},
	'.cm-suggestion-insert': {
		color: 'var(--suggestion-tone-dark)',
		textDecoration: 'underline',
		textDecorationThickness: '1px',
		textUnderlineOffset: '2px',
		userSelect: 'none',
		cursor: 'default',
		// Break between words; one unbreakable token falls back to anywhere.
		wordBreak: 'normal',
		overflowWrap: 'anywhere',
	},
	'.cm-suggestion-insert::after': { content: 'attr(data-insert)' },
	// Presentation only: never part of the document or a copy.
	'.cm-suggestion-insert-after': { marginLeft: '0.25em' },
	'.cm-suggestion-focus': {
		backgroundColor: 'color-mix(in srgb, var(--suggestion-color) 12%, transparent)',
		borderRadius: '2px',
	},
	'html[data-theme="light"] & .cm-suggestion-del, html[data-theme="light"] & .cm-suggestion-insert': {
		color: 'var(--suggestion-tone-light)',
	},
	'html[data-theme="light"] & .cm-suggestion-focus': {
		backgroundColor: 'color-mix(in srgb, var(--suggestion-color) 8%, transparent)',
	},
});

/** The inline marks of open suggestions, derived from `commentRanges`, with the pointer behavior that goes with them. */
export function suggestionMarks(pointer: () => SuggestionPointer): Extension[] {
	return [
		EditorView.decorations.compute([commentRanges], marks),
		theme,
		EditorView.domEventHandlers({
			mousedown(event) {
				const insert = suggestionOf(event.target);
				const id = insert?.classList.contains('cm-suggestion-insert') ? insert.getAttribute(ID_ATTR) : null;
				if (!insert || !id) return false;
				// The insert is not document text: pressing it never moves the caret or starts an edit.
				event.preventDefault();
				pointer().focus(id, insert.getBoundingClientRect());
				return true;
			},
			mouseover(event) {
				const id = suggestionOf(event.target)?.getAttribute(ID_ATTR);
				if (id) pointer().hover(id);
				return false;
			},
			mouseout(event) {
				const id = suggestionOf(event.target)?.getAttribute(ID_ATTR);
				if (id && suggestionOf(event.relatedTarget)?.getAttribute(ID_ATTR) !== id) pointer().hover(null);
				return false;
			},
		}),
	];
}
