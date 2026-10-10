import { afterEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { commentMarks, setCommentRanges, type CommentRange } from './comment-marks';
import { commentRail } from './comment-rail';
import { railWidth } from './comment-clusters';

const views: EditorView[] = [];

function mount(doc: string) {
	const view = new EditorView({ state: EditorState.create({ doc, extensions: [commentMarks, commentRail] }), parent: document.body });
	views.push(view);
	return view;
}

const frame = () => new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));
const buttons = (view: EditorView) => [...view.dom.querySelectorAll<HTMLButtonElement>('.cm-comment-marker')];

async function show(view: EditorView, ranges: CommentRange[]) {
	view.dispatch({ effects: setCommentRanges.of(ranges) });
	await frame();
}

afterEach(() => {
	views.splice(0).forEach((view) => view.destroy());
	document.body.innerHTML = '';
});

describe('comment rail', () => {
	it('draws a glyph button for one thread and a count button for several', async () => {
		const view = mount(`alpha${"\n".repeat(60)}beta`);
		const open = vi.fn();
		await show(view, [{ from: 0, to: 5, id: 'a', onMarkerFocus: open }]);
		const [single] = buttons(view);
		expect(single.getAttribute('aria-label')).toBe('Open comment');
		expect(single.querySelector('svg')).not.toBeNull();
		expect(single.textContent).toBe('');
		single.click();
		expect(open).toHaveBeenCalledWith(['a'], single);
		await show(view, [{ from: 0, to: 2, id: 'a', onMarkerFocus: open }, { from: 2, to: 5, id: 'b', onMarkerFocus: open }]);
		expect(buttons(view)).toHaveLength(1);
		expect(buttons(view)[0].getAttribute('aria-label')).toBe('Open 2 comments');
		expect(buttons(view)[0].textContent).toBe('2');
		expect(buttons(view)[0].dataset.commentIds).toBe('a,b');
	});

	it('marks a waiting reply on a single glyph and on a cluster count, and clears it from both', async () => {
		const view = mount('alpha');
		const open = vi.fn();
		await show(view, [{ from: 0, to: 5, id: 'a', onMarkerFocus: open, replyWaiting: true }]);
		expect(buttons(view)[0].querySelector('circle')).not.toBeNull();
		const cluster = (waiting: boolean) => [
			{ from: 0, to: 2, id: 'a', onMarkerFocus: open, replyWaiting: waiting },
			{ from: 2, to: 5, id: 'b', onMarkerFocus: open },
		];
		await show(view, cluster(true));
		const [count] = buttons(view);
		expect(count.textContent).toBe('2');
		expect(count.querySelector('.cm-comment-dot')).not.toBeNull();
		await show(view, cluster(false));
		expect(buttons(view)[0]).toBe(count);
		expect(count.querySelector('.cm-comment-dot')).toBeNull();
		expect(count.textContent).toBe('2');
	});

	it('keeps the same button, and its focus, when a regroup still holds the focused thread', async () => {
		const view = mount('alpha');
		const open = vi.fn();
		await show(view, [{ from: 0, to: 2, id: 'a', onMarkerFocus: open }, { from: 2, to: 5, id: 'b', onMarkerFocus: open }]);
		const [before] = buttons(view);
		before.focus();
		await show(view, [{ from: 0, to: 2, id: 'a', onMarkerFocus: open }]);
		const [after] = buttons(view);
		expect(after).toBe(before);
		expect(document.activeElement).toBe(before);
		expect(after.dataset.commentIds).toBe('a');
	});

	it('removes a marker whose threads are gone and returns focus to the editor', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout'] });
		try {
			const view = mount('alpha');
			await show(view, [{ from: 0, to: 5, id: 'a' }]);
			buttons(view)[0].focus();
			await show(view, []);
			expect(buttons(view)).toHaveLength(0);
			vi.runAllTimers();
			expect(document.activeElement).toBe(view.contentDOM);
		} finally {
			vi.useRealTimers();
		}
	});

	it('reserves the pointer policy rail on the editor itself, so a mount outside the viewer agrees', () => {
		const view = mount('alpha');
		expect(view.dom.closest('.doc')).toBeNull();
		expect(view.dom.style.getPropertyValue('--doc-rail')).toBe(`${railWidth()}px`);
		view.destroy();
		expect(view.dom.style.getPropertyValue('--doc-rail')).toBe('');
	});

	it('drops the marker together with the highlight when an edit collapses its anchor', async () => {
		const view = mount('alpha\nbeta');
		await show(view, [{ from: 0, to: 5, id: 'a' }, { from: 6, to: 10, id: 'b' }]);
		// jsdom lays out no lines, so both threads share one marker; the collapsed one must leave it.
		expect(buttons(view).map((button) => button.dataset.commentIds)).toEqual(['a,b']);
		view.dispatch({ changes: { from: 0, to: 5 } });
		await frame();
		expect(view.dom.querySelectorAll('.cm-comment-anchor')).toHaveLength(1);
		expect(buttons(view).map((button) => button.dataset.commentIds)).toEqual(['b']);
	});

	it('lists every thread once even when one range is repeated', async () => {
		const view = mount('alpha');
		await show(view, [{ from: 0, to: 3, id: 'a' }, { from: 1, to: 5, id: 'a' }]);
		expect(buttons(view)).toHaveLength(1);
		expect(buttons(view)[0].dataset.commentIds).toBe('a');
	});
});
