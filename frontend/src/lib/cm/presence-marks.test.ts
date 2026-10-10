import { afterEach, describe, expect, it } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { clampPresenceLabelsNow, presenceMarks, revealingThreads, setPresenceRanges, type PresenceRange } from './presence-marks';

let view: EditorView | null = null;
afterEach(() => { view?.destroy(); view = null; });

function mount(ranges: PresenceRange[]) {
	const parent = document.createElement('div');
	document.body.appendChild(parent);
	view = new EditorView({
		parent,
		state: EditorState.create({ doc: 'alpha beta gamma', extensions: [presenceMarks] }),
	});
	view.dispatch({ effects: setPresenceRanges.of(ranges) });
	return parent;
}

describe('presence marks', () => {
	it('renders separate same-title cursors keyed by thread identity', () => {
		const parent = mount([
			{ threadId: 'agent-a', name: 'Reviewer', color: '#f00', span: { from: 0, to: 5 } },
			{ threadId: 'agent-b', name: 'Reviewer', color: '#0f0', span: { from: 6, to: 10 } },
		]);
		const cursors = [...parent.querySelectorAll<HTMLElement>('.cm-presence-cursor')];
		expect(cursors.map((cursor) => cursor.dataset.threadId)).toEqual(['agent-a', 'agent-b']);
		expect(cursors.map((cursor) => cursor.getAttribute('aria-label'))).toEqual([
			'Reviewer recently active here',
			'Reviewer recently active here',
		]);
	});

	it('replaces an agent cursor with its latest span and label', () => {
		const parent = mount([
			{ threadId: 'agent-a', name: 'Old title', color: '#f00', span: { from: 0, to: 5 } },
		]);
		view!.dispatch({ effects: setPresenceRanges.of([
			{ threadId: 'agent-a', name: 'New title', color: '#f00', span: { from: 11, to: 16 }, highlightSpan: { from: 0, to: 5 } },
		]) });
		const cursors = parent.querySelectorAll<HTMLElement>('.cm-presence-cursor');
		expect(cursors).toHaveLength(1);
		expect(cursors[0].dataset.threadId).toBe('agent-a');
		expect(cursors[0].getAttribute('aria-label')).toBe('New title recently active here');
		expect(parent.querySelectorAll('.cm-presence-arrival')).toHaveLength(1);
		let cursorAt = -1;
		let highlightFrom = -1;
		view!.state.field(presenceMarks).between(0, view!.state.doc.length, (from, _to, value) => {
			if (value.spec.class === 'cm-presence-arrival') highlightFrom = from;
			else if (value.spec.widget) cursorAt = from;
		});
		expect(cursorAt).toBe(16);
		expect(highlightFrom).toBe(0);
	});

	it('maps cursors and highlights through local editor changes beside comment marks', () => {
		mount([
			{ threadId: 'agent-a', name: 'Reviewer', color: '#f00', span: { from: 6, to: 10 }, highlightSpan: { from: 6, to: 10 } },
		]);
		view!.dispatch({ changes: { from: 0, insert: 'new ' } });
		const positions: number[] = [];
		view!.state.field(presenceMarks).between(0, view!.state.doc.length, (from) => { positions.push(from); });
		expect(Math.min(...positions)).toBe(10);
	});

	it.each([['edit', 0], ['comment', 1]] as const)('a typing lane hides its own cursor; its %s arrival highlight shows %i time(s)', (kind, kept) => {
		const parent = document.createElement('div');
		document.body.appendChild(parent);
		view = new EditorView({
			parent,
			state: EditorState.create({ doc: 'alpha beta gamma', extensions: [presenceMarks, revealingThreads.of(new Set(['agent-a']))] }),
		});
		view.dispatch({ effects: setPresenceRanges.of([
			{ threadId: 'agent-a', name: 'Reviewer', color: '#f00', span: { from: 6, to: 10 }, highlightSpan: { from: 6, to: 10 }, highlightKind: kind },
			{ threadId: 'agent-b', name: 'Other', color: '#0f0', span: { from: 0, to: 5 }, highlightSpan: { from: 0, to: 5 }, highlightKind: 'edit' },
		]) });
		const threads = (selector: string) => [...parent.querySelectorAll<HTMLElement>(selector)].map((node) => node.dataset.threadId);
		expect(threads('.cm-presence-cursor')).toEqual(['agent-b']);
		expect(threads('.cm-presence-arrival').filter((id) => id === 'agent-a')).toHaveLength(kept);
		expect(threads('.cm-presence-arrival')).toContain('agent-b');
	});

	describe('label clamp', () => {
		/** Lays out the scroller (border box 0..500, client area 0..485 past a 15px scrollbar) and the label's right edge. */
		function layOut(parent: HTMLElement, labelRight: number) {
			const scroller = view!.scrollDOM;
			Object.defineProperty(scroller, 'clientLeft', { configurable: true, value: 0 });
			Object.defineProperty(scroller, 'clientWidth', { configurable: true, value: 485 });
			scroller.getBoundingClientRect = () => new DOMRect(0, 0, 500, 300);
			const label = parent.querySelector<HTMLElement>('.cm-presence-cursor-label')!;
			label.getBoundingClientRect = () => {
				const shift = Number(label.dataset.shift ?? 0);
				return new DOMRect(labelRight - 100 - shift, 0, 100, 16);
			};
			return label;
		}

		it('pulls a label back inside the client area, not the border box over the scrollbar', () => {
			const parent = mount([{ threadId: 'agent-a', name: 'Reviewer', color: '#f00', span: { from: 11, to: 16 } }]);
			const label = layOut(parent, 495);
			clampPresenceLabelsNow(view!);
			expect(label.style.transform).toBe('translateX(-10px)');
			expect(label.dataset.shift).toBe('10');
		});

		it('keeps its shift while the label still overflows and drops it once the caret moves away', () => {
			const parent = mount([{ threadId: 'agent-a', name: 'Reviewer', color: '#f00', span: { from: 11, to: 16 } }]);
			const label = layOut(parent, 495);
			clampPresenceLabelsNow(view!);
			clampPresenceLabelsNow(view!);
			expect(label.style.transform).toBe('translateX(-10px)');
			layOut(parent, 300);
			clampPresenceLabelsNow(view!);
			expect(label.style.transform).toBe('');
			expect(label.dataset.shift).toBe('0');
		});
	});
});
