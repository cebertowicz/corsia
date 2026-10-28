import { flushSync } from 'svelte';
import * as Y from 'yjs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { commentDraft, discardCommentDraft, type CommentDraft } from '$lib/stores/commentDraft';
import { awaitsFirstMark } from './docCanvasComments';
import type { DocComment } from '$lib/stores/docComments';
import { createViewerCommentDraft } from './docViewerCommentDraft.svelte';

vi.mock('$lib/stores/docComments', async (original) => ({
	...await original<typeof import('$lib/stores/docComments')>(),
	addComment: vi.fn(async () => 'posted'),
}));
vi.mock('$lib/stores/commentSelection', async (original) => ({
	...await original<typeof import('$lib/stores/commentSelection')>(),
	captureComment: vi.fn(async (selection: { quote: string }) => ({ from: 0, to: selection.quote.length, quote: selection.quote, clientId: 'client' })),
}));

/*
 * The pending mark of a posted widget comment is held by the controller until one of these exits clears it.
 *
 * | state                          | pendingComment                | leaves when                                                      |
 * | widget selection, not opened   | the selection                 | select(null), start()                                            |
 * | prose selection, not opened    | none (the browser shows it)   | -                                                                |
 * | sheet open                     | the scoped draft's selection  | cancel(), the target changes; a new selection never takes it     |
 * | posted widget comment, waiting | the held selection            | drawn, not_placed, orphaned or gone; cancel(); the target changes |
 * | posted prose comment           | none (its text mark is drawn) | -                                                                |
 */
const session = () => {
	const doc = new Y.Doc(), text = doc.getText('text');
	text.insert(0, 'alpha beta');
	return { artifactId: 'a', doc, text, releaseReservation: vi.fn() } as never;
};
type Row = Pick<DocComment, 'placement' | 'orphaned'> | undefined;

function setup(inWidget: boolean, label?: string) {
	let scope = $state('one'), live = $state.raw(session()), open = $state(false), drawn = $state(false);
	let row = $state<Row>({ placement: 'placing' }), draft = $state.raw<CommentDraft | null>(null);
	const stop = commentDraft.subscribe((value) => { draft = value; });
	let controller!: ReturnType<typeof createViewerCommentDraft>;
	const teardown = $effect.root(() => {
		controller = createViewerCommentDraft({
			scope: () => scope, draft: () => draft, session: () => live, generation: () => 0, text: () => 'alpha beta',
			isOpen: () => open, ensureArtifactId: async () => 'a', onFailure: () => {}, onStart: () => { open = true; },
			onClose: () => { open = false; }, awaitsMark: () => awaitsFirstMark(row as DocComment | undefined, drawn),
		});
	});
	flushSync();
	return {
		controller,
		async post() {
			controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0, label, inWidget });
			controller.start(); flushSync();
			expect(await controller.submit('body')).toBe('posted');
			// The viewer closes the sheet and drops the draft once the comment is posted.
			open = false; commentDraft.set(null); flushSync();
		},
		set(next: { scope?: string; drawn?: boolean; row?: Row; replaceSession?: boolean }) {
			if (next.scope !== undefined) { scope = next.scope; live = session(); }
			if (next.replaceSession) live = session();
			if (next.drawn !== undefined) drawn = next.drawn;
			if ('row' in next) row = next.row;
			flushSync();
		},
		stop() { teardown(); stop(); },
	};
}

let current: ReturnType<typeof setup> | undefined;
afterEach(() => { current?.stop(); current = undefined; discardCommentDraft(); });

describe('a posted widget comment keeps its pending mark', () => {
	it('until its placed mark is drawn', async () => {
		current = setup(true, 'The whole table');
		await current.post();
		expect(current.controller.pendingComment).not.toBeNull();
		current.set({ drawn: true });
		expect(current.controller.pendingComment).toBeNull();
	});

	it('until the comment ends not_placed', async () => {
		current = setup(true, 'The whole table');
		await current.post();
		expect(current.controller.pendingComment).not.toBeNull();
		current.set({ row: { placement: 'not_placed' } });
		expect(current.controller.pendingComment).toBeNull();
	});

	it('until the comment is orphaned', async () => {
		current = setup(true, 'The whole table');
		await current.post();
		current.set({ row: { placement: 'placing', orphaned: true } });
		expect(current.controller.pendingComment).toBeNull();
	});

	it('until cancel()', async () => {
		current = setup(true, 'The whole table');
		await current.post();
		current.controller.cancel(); flushSync();
		expect(current.controller.pendingComment).toBeNull();
	});

	it('until another document is shown', async () => {
		current = setup(true, 'The whole table');
		await current.post();
		current.set({ scope: 'two' });
		expect(current.controller.pendingComment).toBeNull();
		// Back to a row still awaiting its mark: the old session's position stays gone.
		current.set({ row: { placement: 'placing' } });
		expect(current.controller.pendingComment).toBeNull();
	});

	it('whether or not it has a label to show', async () => {
		current = setup(true);
		await current.post();
		expect(current.controller.pendingComment).not.toBeNull();
		current.set({ drawn: true });
		expect(current.controller.pendingComment).toBeNull();
	});

	it('only for a widget comment: a prose comment hands over to its text mark, even one that has a label', async () => {
		current = setup(false, 'A label');
		await current.post();
		expect(current.controller.pendingComment).toBeNull();
	});
});

it('keeps the draft and reports the selection lost when the live session is replaced', () => {
	current = setup(false);
	current.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
	current.controller.start(); flushSync();
	commentDraft.update(draft => draft && { ...draft, body: 'Unfinished' }); flushSync();
	current.set({ replaceSession: true });
	expect(current.controller.draft?.body).toBe('Unfinished');
	expect(current.controller.reason).toContain('The document reconnected');
});

describe('the pending mark before and while the composer is open', () => {
	const selection = (from: number, to: number, inWidget: boolean) => ({ from, to, text: 'alpha beta'.slice(from, to), top: 0, left: 0, inWidget });

	it('is painted for a widget selection and not for prose', () => {
		current = setup(true);
		current.controller.select(selection(0, 5, false)); flushSync();
		expect(current.controller.pendingComment).toBeNull();
		current.controller.select(selection(0, 5, true)); flushSync();
		expect(current.controller.pendingComment).not.toBeNull();
		current.controller.select(null); flushSync();
		expect(current.controller.pendingComment).toBeNull();
	});

	it("stays the open draft's selection when the editor selects something else", () => {
		current = setup(true);
		current.controller.select(selection(0, 5, true));
		current.controller.start(); flushSync();
		const owned = current.controller.pendingComment;
		expect(owned).not.toBeNull();
		current.controller.select(selection(6, 10, true)); flushSync();
		expect(current.controller.pendingComment).toBe(owned);
		current.controller.select(selection(6, 10, false)); flushSync();
		expect(current.controller.pendingComment).toBe(owned);
	});
});
