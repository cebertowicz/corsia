import { describe, expect, it, afterEach, vi } from 'vitest';
import { EditorState, StateEffect } from '@codemirror/state';
import { history, undo } from '@codemirror/commands';
import { insertNewlineContinueMarkup, markdown, markdownKeymap } from '@codemirror/lang-markdown';
import { forceParsing } from '@codemirror/language';
import { GFM } from '@lezer/markdown';
import { EditorView, keymap } from '@codemirror/view';
import { commentMarks, setCommentRanges } from './comment-marks';
import { commentRail } from './comment-rail';
import { markdownLivePreview } from './markdown-live';
import { markdownTasks, toggleTasksOnSelection } from './markdown-tasks';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));
import { emitProductEvent } from '$lib/product_events';

const views: EditorView[] = [];

function mount(doc: string, options: { selection?: { anchor: number; head?: number }; readOnly?: boolean; comments?: boolean } = {}) {
	const state = EditorState.create({
		doc,
		selection: options.selection,
		extensions: [
			history(),
			markdown({ extensions: GFM }),
			keymap.of(markdownKeymap),
			markdownLivePreview,
			markdownTasks,
			...(options.comments ? [commentMarks, commentRail] : []),
			...(options.readOnly ? [EditorState.readOnly.of(true)] : [])
		]
	});
	const view = new EditorView({ state, parent: document.body });
	if (!forceParsing(view, view.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
	views.push(view);
	return view;
}

afterEach(() => {
	while (views.length) views.pop()!.destroy();
	vi.mocked(emitProductEvent).mockClear();
});

describe('markdown tasks', () => {
	it('renders GFM markers as checkboxes and hides unordered bullets', () => {
		const view = mount('- [ ] unpack\n1. [x] ship\n* [ ] star\n+ [x] plus\n');
		const boxes = view.dom.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
		expect(boxes).toHaveLength(4);
		expect(boxes[1].checked).toBe(true);
		expect(boxes[0].getAttribute('aria-label')).toBe('Mark task complete: unpack');
		expect(boxes[1].getAttribute('aria-label')).toBe('Mark task incomplete: ship');
		expect(view.contentDOM.textContent).not.toContain('[ ]');
		expect(view.contentDOM.textContent).not.toContain('[x]');
		expect(view.contentDOM.textContent).not.toContain('•');
		expect(view.contentDOM.textContent).toContain('1.');
	});

	it('draws checkboxes for markers whose parse arrives after the view was built', () => {
		// No language yet, so the first tree is empty; the parser arrives later with no doc or viewport change.
		const view = new EditorView({
			state: EditorState.create({ doc: '- [ ] unpack\n1. [x] ship\n* [ ] star\n+ [x] plus\n', extensions: [markdownTasks] }),
			parent: document.body
		});
		views.push(view);
		expect(view.dom.querySelectorAll('input[type="checkbox"]')).toHaveLength(0);
		view.dispatch({ effects: StateEffect.appendConfig.of(markdown({ extensions: GFM })) });
		if (!forceParsing(view, view.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
		expect(view.dom.querySelectorAll('input[type="checkbox"]')).toHaveLength(4);
	});

	it('uses the parser to distinguish valid task markers from literal source', () => {
		const literal = mount('- [ ]\n');
		expect(literal.dom.querySelectorAll('input[type="checkbox"]')).toHaveLength(0);
		expect(literal.contentDOM.textContent).toContain('•');
		expect(literal.contentDOM.textContent).toContain('[ ]');

		const tasks = mount('-  [ ] spaced\n- [ ] \n');
		expect(tasks.dom.querySelectorAll('input[type="checkbox"]')).toHaveLength(2);
		expect(tasks.contentDOM.textContent).not.toContain('•');
	});

	it('strikes only a checked task item\'s own text, not nested children', () => {
		const view = mount('- [x] parent\n  - [ ] child\n');
		const done = view.dom.querySelector('.cm-md-task-done');
		expect(done?.textContent).toContain('parent');
		expect(done?.textContent).not.toContain('child');
	});

	it('clicking toggles source in one undoable transaction without moving the selection', () => {
		const doc = '- [ ] one\n';
		const cursor = doc.indexOf('one') + 1;
		const view = mount(doc, { selection: { anchor: cursor } });
		view.contentDOM.focus();
		view.dom.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
		expect(view.state.doc.toString()).toBe('- [x] one\n');
		expect(view.state.selection.main.head).toBe(cursor);
		expect(document.activeElement).toBe(view.contentDOM);
		expect(emitProductEvent).toHaveBeenCalledWith('document', 'task_toggled', {
			input: 'click', task_count: 1, completed_count: 1, reopened_count: 0
		});
		view.dom.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
		expect(view.state.doc.toString()).toBe(doc);
		expect(undo(view)).toBe(true);
		expect(view.state.doc.toString()).toBe('- [x] one\n');
		expect(undo(view)).toBe(true);
		expect(view.state.doc.toString()).toBe(doc);
		expect(toggleTasksOnSelection(view)).toBe(true);
		expect(view.state.doc.toString()).toBe('- [x] one\n');
	});

	it('Mod-Enter toggles every task line touched by a selection and falls through otherwise', () => {
		const doc = '- [ ] one\n- [x] two\nplain\n';
		const view = mount(doc, { selection: { anchor: 0, head: doc.indexOf('plain') } });
		expect(toggleTasksOnSelection(view)).toBe(true);
		expect(view.state.doc.toString()).toBe('- [x] one\n- [ ] two\nplain\n');
		view.dispatch({ selection: { anchor: view.state.doc.toString().indexOf('plain') } });
		expect(toggleTasksOnSelection(view)).toBe(false);
	});

	it('does not toggle a read-only task by click or keyboard', () => {
		const view = mount('- [ ] locked\n', { readOnly: true });
		view.dom.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
		expect(view.state.doc.toString()).toBe('- [ ] locked\n');
		expect(toggleTasksOnSelection(view)).toBe(false);
	});

	it('keeps comment decorations anchored across a toggle', () => {
		const doc = '- [ ] annotated\n';
		const view = mount(doc, { comments: true });
		const from = doc.indexOf('annotated');
		view.dispatch({ effects: setCommentRanges.of([{ from, to: from + 'annotated'.length, id: 'comment-1' }]) });
		view.dom.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
		expect(view.dom.querySelector('.cm-comment-anchor')?.textContent).toContain('annotated');
	});

	it('puts the threads of nearby lines in one margin marker and opens them', async () => {
		const view = mount('one comment\nsecond comment\n', { comments: true });
		const onMarkerFocus = vi.fn();
		view.dispatch({ effects: setCommentRanges.of([
			{ from: 0, to: 3, id: 'one', onMarkerFocus },
			{ from: 4, to: 11, id: 'two', onMarkerFocus },
			{ from: 12, to: 18, id: 'three', onMarkerFocus },
		]) });
		await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));
		// No layout in jsdom: both lines sit within one coarse-pointer target height, so one cluster.
		const markers = view.dom.querySelectorAll<HTMLButtonElement>('.cm-comment-marker');
		expect(markers).toHaveLength(1);
		expect(markers[0].textContent).toBe('3');
		markers[0].click();
		expect(onMarkerFocus).toHaveBeenCalledWith(['one', 'two', 'three'], markers[0]);
	});

	it('continues a task as an unchecked task through the existing markdown continuation', () => {
		const doc = '- [x] done';
		const view = mount(doc, { selection: { anchor: doc.length } });
		expect(insertNewlineContinueMarkup(view)).toBe(true);
		expect(view.state.doc.toString()).toBe('- [x] done\n- [ ] ');
	});
});
