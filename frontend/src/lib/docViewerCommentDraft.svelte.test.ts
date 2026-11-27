import { flushSync } from 'svelte';
import { get } from 'svelte/store';
import * as Y from 'yjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { commentDraft, commentDraftScope, discardCommentDraft, type CommentDraft } from '$lib/stores/commentDraft';
import { captureComment, SELECTION_RECONNECTED } from '$lib/stores/commentSelection';
import { addComment } from '$lib/stores/docComments';
import { awaitsFirstMark } from './docCanvasComments';
import type { DocComment } from '$lib/stores/docComments';
import { RequestRefused } from '$lib/api/docs';
import { toasts } from '$lib/stores/toast';
import { documentDraftKey, keptCommentDrafts } from '$lib/stores/keptCommentDrafts.svelte';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { unsentComments } from '$lib/stores/unsentComments.svelte';
import { createMentionSelection } from '$lib/components/comments/mentionSelection';
import { placeOf } from '$lib/keptPlace';
import { createViewerCommentDraft, UNPLACED } from './docViewerCommentDraft.svelte';

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
/** One open file, as the viewer holds it; `keyOf` is where its typed comment is kept. */
const targetOf = (path: string): DocViewerTarget => ({ instanceId: 'tab', path, name: path, projectId: 'p', threadId: null, artifactId: null });
const keyOf = (path: string) => documentDraftKey(targetOf(path));

function setup(inWidget: boolean, label?: string, shown = 'alpha beta') {
	let scope = $state('one'), live = $state.raw(session()), open = $state(false), drawn = $state(false);
	let row = $state<Row>({ placement: 'placing' }), draft = $state.raw<CommentDraft | null>(null), ready = $state(false);
	const stop = commentDraft.subscribe((value) => { draft = value; });
	let controller!: ReturnType<typeof createViewerCommentDraft>;
	const teardown = $effect.root(() => {
		controller = createViewerCommentDraft({
			scope: () => commentDraftScope(targetOf(scope)), target: () => targetOf(scope), draft: () => draft, session: () => live, generation: () => 0, text: () => shown,
			isOpen: () => open, ensureArtifactId: async () => 'a', ready: () => ready, offerFor: (from, to) => ({ from, to, text: shown.slice(from, to), top: 0, left: 0 }), onStart: () => { open = true; },
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
		set(next: { scope?: string; drawn?: boolean; row?: Row; replaceSession?: boolean; ready?: boolean }) {
			if (next.scope !== undefined) { scope = next.scope; live = session(); }
			if (next.ready !== undefined) ready = next.ready;
			if (next.replaceSession) live = session();
			if (next.drawn !== undefined) drawn = next.drawn;
			if ('row' in next) row = next.row;
			flushSync();
		},
		/** Escape: the composer closes and its draft stays. */
		escape() { open = false; flushSync(); },
		stop() { teardown(); stop(); },
	};
}

let current: ReturnType<typeof setup> | undefined;
afterEach(() => { current?.stop(); current = undefined; discardCommentDraft(); keptCommentDrafts.reset(); });

it('posts a non-live first press without the kept sides the server would refuse', async () => {
	current = setup(false, undefined, 'alpha beta');
	current.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
	current.controller.start();
	commentDraft.update(draft => draft && { ...draft, selection: undefined }); flushSync();
	// The server would reject stale kept sides; without them it uses the existing first-press path.
	vi.mocked(addComment).mockImplementationOnce(async (...args) => {
		if (args.length > 12) throw new Error('409 passage_gone');
		return 'posted';
	});
	expect(await current.controller.submit('body')).toBe('posted');
	expect(vi.mocked(addComment).mock.calls.at(-1)).toHaveLength(12);
});

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
	expect(current.controller.reason).toBe(SELECTION_RECONNECTED);
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

describe('a comment refused because the document no longer takes comments', () => {
	const SENTENCE = 'Comments can’t be added to this document. Your comment wasn’t sent, and your text is still here.';
	const refused = () => new RequestRefused(`409 ${JSON.stringify({ detail: SENTENCE, kind: 'not_commentable' })}`, 'not_commentable');
	const begin = (live: boolean) => {
		current = setup(false);
		current.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current.controller.start(); flushSync();
		// The prose composer has a quote and offsets but no live selection.
		commentDraft.update(draft => draft && { ...draft, body: 'Unfinished', ...(live ? {} : { selection: undefined }) }); flushSync();
		vi.mocked(captureComment).mockClear(); vi.mocked(addComment).mockClear();
	};
	const show = vi.spyOn(toasts, 'show');
	afterEach(() => { show.mockReset(); });

	it.each([['selection', true], ['prose', false]])('shows the server sentence once, keeps the %s draft and does not post again', async (_name, live) => {
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		show.mockImplementation(() => 'toast');
		begin(live);
		vi.mocked(addComment).mockRejectedValue(refused());
		await expect(current!.controller.submit('Unfinished')).rejects.toBeInstanceOf(RequestRefused);
		vi.mocked(addComment).mockResolvedValue('posted');
		expect(addComment).toHaveBeenCalledTimes(1);
		expect(captureComment).toHaveBeenCalledTimes(live ? 1 : 0);
		expect(show).toHaveBeenCalledTimes(1);
		expect(show).toHaveBeenCalledWith(SENTENCE, 'error');
		expect(current!.controller.draft?.body).toBe('Unfinished');
		log.mockRestore();
	});
});

describe('a post that lands or fails after its composer closed and another opened', () => {
	// Press Comment, Escape, select again and type more, then the first post ends.
	const begin = async (outcome: 'lands' | 'fails', edit = true) => {
		current = setup(false);
		// The prose path posts under the draft's own id (the live path's capture is mocked to one fixed id).
		const prose = () => commentDraft.update(draft => draft && { ...draft, selection: undefined });
		current.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current.controller.start(); prose(); flushSync();
		current.controller.setBody('Too terse'); flushSync();
		vi.mocked(addComment).mockClear();
		let end!: () => void;
		const held = new Promise<void>(resolve => { end = resolve; });
		vi.mocked(addComment).mockImplementationOnce(async () => { await held; if (outcome === 'fails') throw new Error('500 boom'); return 'posted'; });
		const first = current.controller.submit('Too terse');
		await Promise.resolve();
		current.escape();
		current.controller.select({ from: 6, to: 10, text: 'beta', top: 0, left: 0 });
		current.controller.start(); prose(); flushSync();
		if (edit) { current.controller.setBody('Too terse: say who reads it'); flushSync(); }
		end();
		return first;
	};

	it('lands: the newer text stays, with a fresh id for its own post, and nothing is stored twice', async () => {
		expect(await begin('lands')).toBeNull();
		expect(addComment).toHaveBeenCalledTimes(1);
		expect(current!.controller.draft?.body).toBe('Too terse: say who reads it');
		const kept = keptCommentDrafts.get(keyOf('one'));
		expect(kept?.body).toBe('Too terse: say who reads it');
		expect(kept?.clientId).toBeUndefined();
		const firstId = vi.mocked(addComment).mock.calls[0][7];
		expect(await current!.controller.submit('Too terse: say who reads it')).toBe('posted');
		expect(addComment).toHaveBeenCalledTimes(2);
		expect(vi.mocked(addComment).mock.calls[1][7]).not.toBe(firstId);
		expect(vi.mocked(addComment).mock.calls[1][11]).toBe(false);
	});

	it('lands unedited: the reopened composer is that draft and closes on the stored comment, so nothing is stored twice', async () => {
		const first = await begin('lands', false);
		expect(addComment).toHaveBeenCalledTimes(1);
		expect(keptCommentDrafts.get(keyOf('one'))).toBeUndefined();
		// The press hands the stored id back while the reopened, unedited composer is the one open (the edited case above
		// gets null and stays open). That id is what the viewer closes the open composer on (DocViewer submitComment); the
		// close itself is the viewer's, so this unit test stops at the id.
		expect(current!.controller.draft?.body).toBe('Too terse');
		expect(first).toBe('posted');
	});

	it('lands after its viewer closed: the composer reopened in a new viewer posts unedited under the same id, so the server answers with the stored comment', async () => {
		current = setup(false);
		const prose = () => commentDraft.update(draft => draft && { ...draft, selection: undefined });
		current.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current.controller.start(); prose(); flushSync();
		current.controller.setBody('Too terse'); flushSync();
		vi.mocked(addComment).mockClear();
		let end!: () => void;
		const held = new Promise<void>(resolve => { end = resolve; });
		vi.mocked(addComment).mockImplementationOnce(async () => { await held; return 'posted'; });
		const first = current.controller.submit('Too terse');
		await Promise.resolve();
		current.stop();
		current = setup(false);
		current.controller.select({ from: 6, to: 10, text: 'beta', top: 0, left: 0 });
		current.controller.start(); prose(); flushSync();
		expect(current.controller.draft?.body).toBe('Too terse');
		end(); await first;
		expect(await current.controller.submit('Too terse')).toBe('posted');
		const [earlier, again] = vi.mocked(addComment).mock.calls;
		expect(again[7]).toBe(earlier[7]);
		expect(again[11]).toBe(true);
		expect(earlier).toHaveLength(12);
		expect(again).toHaveLength(12);
	});

	it('lands after its viewer closed, and the reopened press fails: the next press reuses the same id, so the server holds one comment', async () => {
		// The server stores one comment per client id, as its dedupe does.
		const stored = new Map<string, string>();
		const store = (clientId: string) => { if (!stored.has(clientId)) stored.set(clientId, `c${stored.size + 1}`); return stored.get(clientId)!; };
		current = setup(false);
		const prose = () => commentDraft.update(draft => draft && { ...draft, selection: undefined });
		current.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current.controller.start(); prose(); flushSync();
		current.controller.setBody('Too terse'); flushSync();
		vi.mocked(addComment).mockClear();
		let end!: () => void;
		const held = new Promise<void>(resolve => { end = resolve; });
		vi.mocked(addComment).mockImplementationOnce(async (...args) => { await held; return store(args[7] as string); });
		const first = current.controller.submit('Too terse');
		await Promise.resolve();
		current.stop();
		current = setup(false);
		current.controller.select({ from: 6, to: 10, text: 'beta', top: 0, left: 0 });
		current.controller.start(); prose(); flushSync();
		end(); await first;
		vi.mocked(addComment).mockRejectedValueOnce(new Error('500 boom'));
		await expect(current.controller.submit('Too terse')).rejects.toThrow('500');
		vi.mocked(addComment).mockImplementationOnce(async (...args) => store(args[7] as string));
		expect(await current.controller.submit('Too terse')).toBe('c1');
		const [earlier, failed, again] = vi.mocked(addComment).mock.calls;
		expect(failed[7]).toBe(earlier[7]);
		expect(again[7]).toBe(earlier[7]);
		expect(failed).toHaveLength(12);
		expect(again).toHaveLength(12);
		expect(stored.size).toBe(1);
		expect(keptCommentDrafts.get(keyOf('one'))).toBeUndefined();
	});

	it('lands while its text and tags were edited: the kept edit keeps both, whatever the composer that pressed clears', async () => {
		current = setup(false);
		current.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current.controller.start(); commentDraft.update(draft => draft && { ...draft, selection: undefined }); flushSync();
		current.controller.setBody('Ask'); flushSync();
		let end!: () => void;
		const held = new Promise<void>(resolve => { end = resolve; });
		vi.mocked(addComment).mockImplementationOnce(async () => { await held; return 'posted'; });
		const first = current.controller.submit('Ask');
		const tags = current.controller.draft!.mentionSelection!;
		tags.update('Ask @Ada'); tags.select('agent-ada', 4, '@Ada');
		current.controller.setBody('Ask @Ada'); flushSync();
		end(); await first;
		// The composer that pressed empties its own tags once its post is stored (CommentComposer).
		tags.clear();
		const kept = keptCommentDrafts.get(keyOf('one'))!;
		expect(kept.body).toBe('Ask @Ada');
		expect(kept.mentionSelection.occurrences(kept.body).map((item) => item.agent_id)).toEqual(['agent-ada']);
	});

	it('fails: what the field holds now is kept with the reason, not the text that was sent', async () => {
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		await expect(begin('fails')).rejects.toThrow('500');
		expect(keptCommentDrafts.get(keyOf('one'))).toMatchObject({ body: 'Too terse: say who reads it' });
		expect(keptCommentDrafts.get(keyOf('one'))?.error).toBeTruthy();
		expect(current!.controller.draft?.body).toBe('Too terse: say who reads it');
		log.mockRestore();
	});
});

/** A passage that cannot be told apart: another copy of `alpha` has the same text after it, so the draft keeps no place. */
const REPEATED = 'alpha, and the words that come after it. alpha, and the words that come after it.';

// A draft with no place does not go on its own, so a failure that passes is told as before.
describe('a failed post says why in one place, decided as it fails', () => {
	const REASON = 'Couldn’t post the comment. Your draft is kept. Try again.';
	const show = vi.spyOn(toasts, 'show');
	let drop = () => {};
	/** The composer's root, mounted on screen for this document's draft (CommentComposer `use:showsDraft`). */
	const composerOnScreen = () => {
		const node = document.createElement('div');
		node.getBoundingClientRect = () => new DOMRect(20, 100, 300, 160);
		drop = keptCommentDrafts.shown(keyOf('one'), node);
	};
	const open = () => {
		current!.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current!.controller.start(); commentDraft.update(draft => draft && { ...draft, selection: undefined }); flushSync();
	};
	const held = () => {
		let end!: () => void;
		const wait = new Promise<void>(resolve => { end = resolve; });
		vi.mocked(addComment).mockImplementationOnce(async () => { await wait; throw new Error('500 boom'); });
		return () => end();
	};
	let log: ReturnType<typeof vi.spyOn>;
	beforeEach(() => { show.mockReset(); show.mockImplementation(() => 'toast'); log = vi.spyOn(console, 'error').mockImplementation(() => {}); current = setup(false, undefined, REPEATED); });
	afterEach(() => { drop(); drop = () => {}; log.mockRestore(); show.mockReset(); });

	it('in the open composer, with no toast, while the composer is on screen', async () => {
		open(); composerOnScreen();
		current!.controller.setBody('Too terse'); flushSync();
		vi.mocked(addComment).mockRejectedValueOnce(new Error('500 boom'));
		await expect(current!.controller.submit('Too terse')).rejects.toThrow('500');
		expect(show).not.toHaveBeenCalled();
		expect(keptCommentDrafts.get(keyOf('one'))).toMatchObject({ error: REASON, toast: undefined });
		expect(current!.controller.draft?.error).toBe(REASON);
	});

	it('names the server\'s reason for a refusal, as a live document does, and keeps the generic line for a 409', async () => {
		open(); composerOnScreen();
		current!.controller.setBody('Too terse'); flushSync();
		const gone = '400 {"detail":"the image shots/a.png couldn’t be found.","kind":"region_target"}';
		vi.mocked(addComment).mockRejectedValueOnce(new Error(gone));
		await expect(current!.controller.submit('Too terse')).rejects.toThrow('400');
		const reason = 'Couldn’t post the comment: the image shots/a.png couldn’t be found. Your draft is kept.';
		expect(current!.controller.draft?.error).toBe(reason);
		vi.mocked(addComment).mockRejectedValueOnce(new Error('409 {"detail":"client_id collides with another artifact\'s comment"}'));
		await expect(current!.controller.submit('Too terse')).rejects.toThrow('409');
		expect(current!.controller.draft?.error).toBe(REASON);
	});

	it('in a toast when the composer closed while it posted; the composer opened again says why', async () => {
		open(); composerOnScreen();
		current!.controller.setBody('Too terse'); flushSync();
		const end = held();
		const posting = current!.controller.submit('Too terse');
		await Promise.resolve();
		current!.escape(); drop(); commentDraft.set(null); flushSync();
		end();
		await expect(posting).rejects.toThrow('500');
		expect(show).toHaveBeenCalledExactlyOnceWith(REASON, 'error');
		// The kept draft holds the toast that told it.
		expect(keptCommentDrafts.get(keyOf('one'))).toMatchObject({ error: REASON, toast: 'toast' });
		const dismiss = vi.spyOn(toasts, 'dismiss').mockImplementation(() => {});
		try {
			open();
			expect(current!.controller.draft).toMatchObject({ body: 'Too terse', error: REASON });
			// Its box on screen again says why, so the toast goes: the reason is told in one place.
			composerOnScreen();
			expect(dismiss).toHaveBeenCalledExactlyOnceWith('toast');
			expect(keptCommentDrafts.get(keyOf('one'))?.toast).toBeUndefined();
		} finally {
			dismiss.mockRestore();
		}
	});

	it('in the composer opened again from the kept draft while it posted, when that one is on screen', async () => {
		open(); composerOnScreen();
		current!.controller.setBody('Too terse'); flushSync();
		const end = held();
		const posting = current!.controller.submit('Too terse');
		await Promise.resolve();
		current!.escape(); drop();
		current!.controller.select({ from: 6, to: 10, text: 'beta', top: 0, left: 0 });
		current!.controller.start(); commentDraft.update(draft => draft && { ...draft, selection: undefined }); flushSync();
		composerOnScreen();
		end();
		await expect(posting).rejects.toThrow('500');
		expect(show).not.toHaveBeenCalled();
		expect(current!.controller.draft).toMatchObject({ body: 'Too terse', error: REASON });
	});

	/** The press goes out, its composer closes, and `next` is the composer open on the page when the post fails. */
	const failWhileOpen = async (next: { scope: string; key: string }) => {
		open(); composerOnScreen();
		current!.controller.setBody('Too terse'); flushSync();
		const end = held();
		const posting = current!.controller.submit('Too terse');
		await Promise.resolve();
		current!.escape(); drop();
		commentDraft.set({ from: 0, to: 1, text: 'x', top: 0, body: 'Shown there', identity: {}, ...next }); flushSync();
		const node = document.createElement('div');
		node.getBoundingClientRect = () => new DOMRect(20, 100, 300, 160);
		drop = keptCommentDrafts.shown(next.key, node);
		end();
		await expect(posting).rejects.toThrow('500');
	};

	it('in the composer showing this draft in another viewer of the same document, matched by the draft\'s key', async () => {
		await failWhileOpen({ scope: 'another viewer of one', key: keyOf('one') });
		expect(show).not.toHaveBeenCalled();
		expect(get(commentDraft)?.error).toBe(REASON);
	});

	it('never in a composer showing another document\'s draft: that one is not on screen for this post, so a toast says why', async () => {
		await failWhileOpen({ scope: 'two', key: keyOf('two') });
		expect(show).toHaveBeenCalledExactlyOnceWith(REASON, 'error');
		expect(get(commentDraft)?.error).toBeUndefined();
		expect(keptCommentDrafts.get(keyOf('one'))?.error).toBe(REASON);
	});
});

describe('a kept draft with a passage comes back in its box once its document is shown', () => {
	const body = 'Say who reads it';
	const keep = (shown: string) => {
		const tags = createMentionSelection(body);
		keptCommentDrafts.keep(keyOf('one'), body, tags, placeOf('alpha beta', 0, 5));
		keptCommentDrafts.claim(keyOf('one'), body, tags, undefined, { artifactId: 'a', threadId: null });
		current = setup(false, undefined, shown);
	};

	it('not before the document is shown with its live text', () => {
		keep('alpha beta');
		expect(current!.controller.draft).toBeNull();
	});

	it('on its passage, sending, when the passage is found as kept', () => {
		keep('alpha beta');
		current!.set({ ready: true });
		expect(current!.controller.draft).toMatchObject({ body, from: 0, to: 5, text: 'alpha' });
		expect(current!.controller.draft?.unplaced).toBeUndefined();
		expect(unsentComments.sending(keyOf('one'))).toBe(true);
	});

	it('on no text, with why, and not sent, when its passage changed before it went', () => {
		keep('gamma beta');
		current!.set({ ready: true });
		expect(current!.controller.draft).toMatchObject({ body, unplaced: true, error: UNPLACED });
		expect(unsentComments.sending(keyOf('one'))).toBe(false);
	});
});

describe('a pressed comment on a passage it can be told by', () => {
	let log: ReturnType<typeof vi.spyOn>;
	const show = vi.spyOn(toasts, 'show');
	const key = keyOf('one');
	const press = (failure: Error) => {
		current!.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current!.controller.start(); commentDraft.update(draft => draft && { ...draft, selection: undefined }); flushSync();
		current!.controller.setBody('Too terse'); flushSync();
		vi.mocked(addComment).mockRejectedValueOnce(failure);
		return current!.controller.submit('Too terse');
	};
	beforeEach(() => { show.mockReset(); show.mockImplementation(() => 'toast'); log = vi.spyOn(console, 'error').mockImplementation(() => {}); current = setup(false); });
	afterEach(() => { log.mockRestore(); show.mockReset(); });

	it('reads "Sending…" when its post fails for a reason that passes: no reason, no toast, and it goes again on its own', async () => {
		await expect(press(new Error('503 boom'))).rejects.toThrow('503');
		expect(show).not.toHaveBeenCalled();
		expect(keptCommentDrafts.get(key)?.error).toBeUndefined();
		expect(current!.controller.draft?.error).toBeUndefined();
		expect(unsentComments.sending(key)).toBe(true);
	});

	it('says nothing either when its box closed while it posted', async () => {
		let end!: () => void;
		const wait = new Promise<void>(resolve => { end = resolve; });
		current!.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current!.controller.start(); commentDraft.update(draft => draft && { ...draft, selection: undefined }); flushSync();
		current!.controller.setBody('Too terse'); flushSync();
		vi.mocked(addComment).mockImplementationOnce(async () => { await wait; throw new Error('504 timeout'); });
		const posting = current!.controller.submit('Too terse');
		await Promise.resolve();
		current!.escape(); commentDraft.set(null); flushSync();
		end();
		await expect(posting).rejects.toThrow('504');
		expect(show).not.toHaveBeenCalled();
		expect(unsentComments.sending(key)).toBe(true);
	});

	it('is told why when the server refuses it, and then waits for the person', async () => {
		await expect(press(new Error('400 {"detail":"the image shots/a.png couldn’t be found.","kind":"region_target"}'))).rejects.toThrow('400');
		expect(current!.controller.draft?.error).toBe('Couldn’t post the comment: the image shots/a.png couldn’t be found. Your draft is kept.');
		expect(unsentComments.sending(key)).toBe(false);
	});
});

describe('a pressed comment on a passage it cannot be told by', () => {
	it('shows the failure line when its post fails for a reason that passes, keeps Comment, and does not go on its own', async () => {
		const log = vi.spyOn(console, 'error').mockImplementation(() => {});
		current = setup(false, undefined, REPEATED);
		current.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current.controller.start(); commentDraft.update(draft => draft && { ...draft, selection: undefined }); flushSync();
		current.controller.setBody('Too terse'); flushSync();
		vi.mocked(addComment).mockRejectedValueOnce(new Error('500 boom'));
		await expect(current.controller.submit('Too terse')).rejects.toThrow('500');
		expect(current.controller.draft?.error).toBe('Couldn’t post the comment. Your draft is kept. Try again.');
		expect(unsentComments.sending(keyOf('one'))).toBe(false);
		// Comment stays pressable: the next press posts under the same draft.
		expect(await current.controller.submit('Too terse')).toBe('posted');
		log.mockRestore();
	});
});

describe('a save the server refuses because the document changed', () => {
	const begin = () => {
		current = setup(false);
		current.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current.controller.start(); flushSync();
		vi.mocked(captureComment).mockClear(); vi.mocked(addComment).mockClear();
	};

	it('is captured again from the still-tracked selection and posts without the person retrying', async () => {
		begin();
		vi.mocked(addComment).mockRejectedValueOnce(new Error('409 The text you selected changed'));
		expect(await current!.controller.submit('body')).toBe('posted');
		expect(captureComment).toHaveBeenCalledTimes(2);
		expect(addComment).toHaveBeenCalledTimes(2);
	});

	it('fails with the existing message only when the second attempt is refused too', async () => {
		begin();
		vi.mocked(addComment).mockRejectedValue(new Error('409 The text you selected changed'));
		await expect(current!.controller.submit('body')).rejects.toThrow('409');
		expect(addComment).toHaveBeenCalledTimes(2);
		vi.mocked(addComment).mockResolvedValue('posted');
	});

	it('is not captured again for any other failure', async () => {
		begin();
		vi.mocked(addComment).mockRejectedValueOnce(new Error('500 boom'));
		await expect(current!.controller.submit('body')).rejects.toThrow('500');
		expect(captureComment).toHaveBeenCalledTimes(1);
	});
});

describe('the viewer asks whether a typed comment is waiting', () => {
	const typed = (body: string) => {
		current = setup(false);
		current.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current.controller.start(); flushSync();
		current.controller.setBody(body); flushSync();
	};

	it('only for this file\'s draft with text in it', () => {
		typed('Say who reads it');
		expect(current!.controller.hasText).toBe(true);
	});

	it('not for a body of only spaces', () => {
		typed('  \n ');
		expect(current!.controller.hasText).toBe(false);
	});

	it('not for another file\'s draft', () => {
		typed('Say who reads it');
		const other = get(commentDraft);
		current!.set({ scope: 'two' });
		// Another viewer's draft, with text, is what the store holds while this one shows file two.
		commentDraft.set(other); flushSync();
		expect(get(commentDraft)?.body).toBe('Say who reads it');
		expect(current!.controller.draft).toBeNull();
		expect(current!.controller.hasText).toBe(false);
	});
});

describe('the viewer moves to another file', () => {
	it('lets go of the last file\'s open draft and keeps its typed text', () => {
		current = setup(false);
		current.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current.controller.start(); flushSync();
		current.controller.setBody('Say who reads it'); flushSync();
		current.controller.leave(targetOf('one'), commentDraftScope(targetOf('two')));
		flushSync();
		expect(get(commentDraft)).toBeNull();
		expect(keptCommentDrafts.get(keyOf('one'))?.body).toBe('Say who reads it');
	});

	it('keeps the open draft when the same file loads again', () => {
		current = setup(false);
		current.controller.select({ from: 0, to: 5, text: 'alpha', top: 0, left: 0 });
		current.controller.start(); flushSync();
		current.controller.setBody('Say who reads it'); flushSync();
		current.controller.leave(targetOf('one'), commentDraftScope(targetOf('one')));
		flushSync();
		expect(get(commentDraft)?.body).toBe('Say who reads it');
	});
});
