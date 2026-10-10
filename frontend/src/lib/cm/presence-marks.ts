import { Facet, StateEffect, StateField } from '@codemirror/state';
import { Decoration, EditorView, ViewPlugin, WidgetType, type DecorationSet } from '@codemirror/view';
import type { DocActivityKind, DocSpan } from '$lib/docPresence';

export interface PresenceRange {
	threadId: string;
	name: string;
	color: string;
	span: DocSpan;
	highlightSpan?: DocSpan | null;
	highlightKind?: DocActivityKind | null;
	/** Ends this lane's wait and shows its pending arrival now, as any user click in an arrival highlight does. */
	revealPending?: () => void;
}

export const setPresenceRanges = StateEffect.define<PresenceRange[]>();

/** Threads whose caret and arrival highlight a typed reveal draws instead. */
export const revealingThreads = Facet.define<ReadonlySet<string>, ReadonlySet<string>>({
	combine: (values) => values[0] ?? new Set(),
});

/** The agent caret. It blinks while idle and holds steady while `typing`. */
export class PresenceCursor extends WidgetType {
	constructor(
		readonly threadId: string,
		private readonly name: string,
		private readonly color: string,
		private readonly typing = false,
		private readonly quiet = false,
	) { super(); }

	eq(other: PresenceCursor): boolean {
		return this.threadId === other.threadId && this.name === other.name
			&& this.color === other.color && this.typing === other.typing && this.quiet === other.quiet;
	}

	toDOM(): HTMLElement {
		const cursor = document.createElement('span');
		const states = [this.typing ? 'cm-presence-cursor-typing' : '', this.quiet ? 'cm-presence-cursor-quiet' : ''];
		cursor.className = ['cm-presence-cursor', ...states].filter(Boolean).join(' ');
		cursor.dataset.threadId = this.threadId;
		cursor.style.setProperty('--presence-color', this.color);
		cursor.setAttribute('aria-label', `${this.name} recently active here`);
		const label = document.createElement('span');
		label.className = 'cm-presence-cursor-label';
		label.textContent = this.name;
		label.setAttribute('aria-hidden', 'true');
		cursor.appendChild(label);
		return cursor;
	}

	ignoreEvent(): boolean { return true; }
}

const pendingReveals = StateField.define<ReadonlyMap<string, () => void>>({
	create: () => new Map(),
	update(current, transaction) {
		for (const effect of transaction.effects) {
			if (!effect.is(setPresenceRanges)) continue;
			return new Map(effect.value.flatMap((range) => range.highlightSpan && range.revealPending
				? [[range.threadId, range.revealPending] as const] : []));
		}
		return current;
	},
});

/** Clicking an anchor whose comment has not appeared yet shows the comment at once. */
const anchorClick = EditorView.domEventHandlers({
	click(event, view) {
		const anchor = event.target instanceof Element ? event.target.closest<HTMLElement>('.cm-presence-arrival') : null;
		const threadId = anchor?.dataset.threadId;
		if (threadId) view.state.field(pendingReveals).get(threadId)?.();
		return false;
	},
});

function decorations(ranges: PresenceRange[], docLength: number): DecorationSet {
	const marks = ranges.flatMap((range) => {
		const cursorFrom = Math.min(Math.max(0, range.span.from), docLength);
		const cursorTo = Math.min(Math.max(cursorFrom, range.span.to), docLength);
		const cursor = Decoration.widget({
			widget: new PresenceCursor(range.threadId, range.name, range.color),
			side: 1,
		}).range(cursorTo);
		if (!range.highlightSpan) return [cursor];
		const highlightFrom = Math.min(Math.max(0, range.highlightSpan.from), docLength);
		const highlightTo = Math.min(Math.max(highlightFrom, range.highlightSpan.to), docLength);
		if (highlightFrom === highlightTo) return [cursor];
		const highlight = Decoration.mark({
			class: 'cm-presence-arrival',
			attributes: {
				style: `--presence-color:${range.color}`,
				'data-thread-id': range.threadId,
			},
			kind: range.highlightKind ?? null,
		}).range(highlightFrom, highlightTo);
		return [highlight, cursor];
	});
	return Decoration.set(marks, true);
}

const presenceTheme = EditorView.baseTheme({
	'.cm-presence-arrival': {
		backgroundColor: 'color-mix(in srgb, var(--presence-color) 18%, transparent)',
		borderRadius: '2px',
		transition: 'background-color 160ms ease-out',
	},
	'.cm-presence-cursor': {
		position: 'relative',
		display: 'inline-block',
		width: '2px',
		height: '1.2em',
		margin: '0 -1px',
		verticalAlign: 'text-bottom',
		backgroundColor: 'var(--presence-color)',
		pointerEvents: 'none',
		animation: 'cm-presence-blink 1s steps(1, end) infinite',
	},
	'.cm-presence-cursor-typing': { animation: 'none' },
	'.cm-presence-cursor::before': {
		content: '""',
		position: 'absolute',
		top: '0',
		bottom: '0',
		// The hover target reaches left only: past the caret's right it would overflow a line that
		// ends at the editor's edge and scroll the editor sideways.
		left: '-9px',
		right: '0',
		pointerEvents: 'auto',
	},
	'.cm-presence-cursor-quiet .cm-presence-cursor-label': { opacity: '0', transition: 'opacity 300ms ease-out' },
	'.cm-presence-cursor-quiet:hover .cm-presence-cursor-label': { opacity: '1' },
	'@keyframes cm-presence-blink': {
		'0%, 50%': { backgroundColor: 'var(--presence-color)' },
		'50.01%, 100%': { backgroundColor: 'transparent' },
	},
	'.cm-presence-cursor-label': {
		position: 'absolute',
		left: '0',
		bottom: '100%',
		maxWidth: '140px',
		padding: '1px 5px',
		borderRadius: '4px 4px 4px 0',
		backgroundColor: 'var(--presence-color)',
		color: '#fff',
		fontSize: '10px',
		fontWeight: '600',
		lineHeight: '16px',
		whiteSpace: 'nowrap',
		overflow: 'hidden',
		textOverflow: 'ellipsis',
	},
	'@media (prefers-reduced-motion: reduce)': {
		'.cm-presence-arrival': { transition: 'none' },
		'.cm-presence-cursor': { animation: 'none' },
	},
});

/** Keep every caret label inside the editor so a label near the right edge never scrolls the page sideways. */
const labelClamp = {
	key: 'presence-label-clamp',
	read(view: EditorView) {
		// The client area, not the border box: a label over the scrollbar gutter still scrolls sideways.
		const scroller = view.scrollDOM;
		const limit = scroller.getBoundingClientRect().left + scroller.clientLeft + scroller.clientWidth;
		return Array.from(view.dom.querySelectorAll<HTMLElement>('.cm-presence-cursor-label')).map((label) => {
			const shift = Number(label.dataset.shift ?? 0);
			return { label, shift, overflow: Math.max(0, label.getBoundingClientRect().right + shift - limit) };
		});
	},
	write(rows: Array<{ label: HTMLElement; shift: number; overflow: number }>) {
		for (const { label, shift, overflow } of rows) {
			if (overflow === shift) continue;
			label.style.transform = overflow ? `translateX(${-overflow}px)` : '';
			label.dataset.shift = String(overflow);
		}
	},
};

const clampLabels = ViewPlugin.define((view) => ({ update: () => view.requestMeasure(labelClamp) }));

/**
 * Clamp now, before this frame paints. A dispatch made inside an animation-frame callback
 * (the typing driver) would otherwise paint its new caret before the next measure frame clamps it.
 */
export function clampPresenceLabelsNow(view: EditorView): void {
	labelClamp.write(labelClamp.read(view));
}

export const presenceMarks = StateField.define<DecorationSet>({
	create: () => Decoration.none,
	update(current, transaction) {
		let next = current.map(transaction.changes);
		for (const effect of transaction.effects) {
			if (effect.is(setPresenceRanges)) next = decorations(effect.value, transaction.state.doc.length);
		}
		return next;
	},
	provide: (field) => [
		EditorView.decorations.compute([field, revealingThreads], (state) => {
			const hidden = state.facet(revealingThreads);
			const marks = state.field(field);
			if (!hidden.size) return marks;
			return marks.update({
				filter: (_from, _to, value) => {
					if (value.spec.widget instanceof PresenceCursor) return !hidden.has(value.spec.widget.threadId);
					// A pending comment anchor sits only on revealed text (the editor reveals under it) and stays clickable;
					// any other arrival highlight of a typing lane would cover the text it is still typing.
					return value.spec.kind === 'comment' || !hidden.has(value.spec.attributes?.['data-thread-id']);
				},
			});
		}),
		presenceTheme,
		clampLabels,
		pendingReveals,
		anchorClick,
	],
});
