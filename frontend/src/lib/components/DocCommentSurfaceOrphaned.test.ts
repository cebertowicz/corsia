import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { fromStore, get, writable } from 'svelte/store';
import { ChangeSet } from '@codemirror/state';

// Only the network is replaced: the comment store, the refusal store, the orphaned rule and the card are the real ones.
const mocks = vi.hoisted(() => ({
	decideSuggestion: vi.fn(), flushDraft: vi.fn(), seedDraft: vi.fn(), fetchComments: vi.fn(), persistPositions: vi.fn(),
}));
vi.mock('$lib/api/commentSuggestions', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/api/commentSuggestions')>(), decideSuggestion: mocks.decideSuggestion,
}));
vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: mocks.flushDraft, seedDraft: mocks.seedDraft }));
vi.mock('$lib/api/docs', () => ({
	fetchComments: mocks.fetchComments, postComment: vi.fn(), patchComment: vi.fn(), persistPositions: mocks.persistPositions,
}));

import DocCommentSurface from './DocCommentSurface.svelte';
import { canvasCommentHighlights, canvasCommentRange, commentsInDocumentOrder } from '$lib/docCanvasComments';
import { commentLoads, docComments, flushPositions, loadComments, remapComments, type DocComment } from '$lib/stores/docComments';
import { suggestionRefusals } from '$lib/stores/suggestionRefusals';
import { SuggestionRefused } from '$lib/api/commentSuggestions';

const ARTIFACT = 'artifact-1';
const START = 'The quick brown fox.';
const STALE = 'The text this suggestion replaces is no longer in the document, so it cannot be applied.';
// What the server sends when the saved file no longer matches the suggestion.
const refusedAsStale = () => new SuggestionRefused(STALE, 409, 'stale');
const target = { instanceId: 'tab-1', path: 'notes.md', name: 'Notes', projectId: 'project-1', threadId: 'thread-1', artifactId: ARTIFACT };
// A suggestion on "quick". The server judges `orphaned` against the saved file, which can lag the screen.
const serverRow = (orphaned: boolean, overrides: Record<string, unknown> = {}) => ({
	id: 'comment-1', author: 'agent-1', author_kind: 'agent', body: 'Prefer this wording', anchor_text: 'quick',
	from_off: 4, to_off: 9, orphaned, resolved: 0, replacement: 'slow', replies: [], created_at: '2026-10-02T00:00:00Z', ...overrides,
});

const screenText = writable(START);
const screen = fromStore(screenText);
const stored = fromStore(docComments);
const refusals = fromStore(suggestionRefusals);
// What DocViewer hands the surface: the stored comments judged against the text on screen and this document's refusals.
const listed = () => commentsInDocumentOrder(stored.current[ARTIFACT] ?? [], screen.current, refusals.current[ARTIFACT]);
const surface = writable<{ kind: 'thread'; id: string } | { kind: 'list' }>({ kind: 'thread', id: 'comment-1' });
const surfaceNow = fromStore(surface);
let host: HTMLElement;
let component: Record<string, unknown> | null = null;

async function serverAnswers(...rows: ReturnType<typeof serverRow>[]) {
	mocks.fetchComments.mockResolvedValueOnce({ comments: rows, feedback: {} });
	await loadComments(ARTIFACT);
	flushSync();
}
function openCard() {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(DocCommentSurface, {
		target: host,
		props: Object.defineProperties({
			state: { kind: 'thread', id: 'comment-1' }, anchor: document.createElement('button'), narrow: false,
			comments: [] as DocComment[], focusedId: null, draft: null, onClose: vi.fn(), restoreFocus: vi.fn(), onFocus: vi.fn(), onSubmit: vi.fn(),
			onReply: vi.fn(), onResolve: vi.fn(), onAgree: vi.fn(), target, artifactId: ARTIFACT,
		}, Object.getOwnPropertyDescriptors({
			get state() { return surfaceNow.current; },
			get comments() { return listed(); },
		})),
	});
	flushSync();
}
// The user's edit, as DocViewer applies it: the screen text changes and the editor's position map goes to the store.
function edit(from: number, to: number, insert: string) {
	const before = get(screenText);
	const change = ChangeSet.of({ from, to, insert }, before.length);
	remapComments(ARTIFACT, (position) => change.mapPos(position));
	screenText.set(before.slice(0, from) + insert + before.slice(to));
	flushSync();
}
const group = () => host.querySelector('h3')?.textContent;
const accept = () => host.querySelector<HTMLButtonElement>('.ct-suggestion-accept')!;
const alert = () => host.querySelector('[role="alert"]')?.textContent ?? null;
const expectAttached = () => {
	expect(group()).toBe('Open comments');
	expect(accept().disabled).toBe(false);
	expect(host.querySelector('.ct-orphan')).toBeNull();
};
const expectOrphaned = () => {
	expect(group()).toBe('Text changed');
	expect(accept().disabled).toBe(true);
	expect(host.querySelector('.ct-orphan')?.textContent).toBe('The text this suggestion replaces has changed.');
};
async function refuseAccept() {
	mocks.flushDraft.mockResolvedValue(true);
	mocks.decideSuggestion.mockRejectedValue(refusedAsStale());
	accept().click();
	await vi.waitFor(() => expect(alert()).toBe(STALE));
}
// A comments load from anywhere (another decision, a change frame, Retry), answering for the saved file.
async function anotherLoad(outcome: 'succeeds' | 'fails', orphaned = true) {
	if (outcome === 'fails') mocks.fetchComments.mockRejectedValueOnce(new Error('Offline'));
	else mocks.fetchComments.mockResolvedValueOnce({ comments: [serverRow(orphaned)], feedback: {} });
	await loadComments(ARTIFACT).catch(() => undefined);
	flushSync();
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	docComments.set({}); commentLoads.set({}); suggestionRefusals.set({}); screenText.set(START);
	surface.set({ kind: 'thread', id: 'comment-1' });
	vi.resetAllMocks();
});

describe('a suggestion card and the text it replaces', () => {
	it('keeps a comment on unsaved typing attached through a load that says the saved file lacks it', async () => {
		// The user types a sentence and comments on it before the draft saves; the saved file has neither yet.
		edit(START.length, START.length, ' Very new.');
		const typed = serverRow(true, { id: 'typed', anchor_text: 'new', from_off: 26, to_off: 29, replacement: 'fresh' });
		await serverAnswers(serverRow(false), { ...typed, orphaned: false });
		openCard();
		// Rejecting another suggestion reloads comments without saving the draft.
		mocks.decideSuggestion.mockResolvedValue({ comment: { id: 'comment-1' } });
		mocks.fetchComments.mockResolvedValueOnce({ comments: [serverRow(true, { resolved: 1 }), typed], feedback: {} });
		host.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!.click();
		await vi.waitFor(() => expect(mocks.fetchComments).toHaveBeenCalledTimes(2));
		flushSync();
		surface.set({ kind: 'thread', id: 'typed' }); flushSync();
		expectAttached();
		const card = listed().find((comment) => comment.id === 'typed')!;
		expect(canvasCommentHighlights(listed(), get(screenText), null, undefined).some((mark) => mark.id === 'typed')).toBe(true);
		expect(canvasCommentRange(card, get(screenText))).toEqual({ from: 26, to: 29 });
	});

	it('turns orphaned after a refused accept on a stale screen, with the reason and no reload', async () => {
		await serverAnswers(serverRow(false));
		openCard();
		expectAttached();
		// The saved file changed underneath: the screen still shows "quick", the server no longer finds it.
		await refuseAccept();
		expect(get(screenText)).toBe(START);
		expectOrphaned();
		expect(mocks.fetchComments).toHaveBeenCalledOnce();
	});

	it.each(['succeeds', 'fails'] as const)('keeps a refused card orphaned with its reason when a later load %s', async (outcome) => {
		await serverAnswers(serverRow(false));
		openCard();
		await refuseAccept();
		await anotherLoad(outcome, false);
		expectOrphaned();
		expect(alert()).toBe(STALE);
	});

	it('re-attaches when the user deletes a letter of the quote and types it back', async () => {
		await serverAnswers(serverRow(false));
		openCard();
		edit(6, 7, '');
		expect(get(screenText)).toBe('The quck brown fox.');
		expectOrphaned();
		edit(6, 6, 'i');
		expect(get(screenText)).toBe(START);
		expectAttached();
	});

	it("follows the screen, not the server's answer, while the quote is on screen", async () => {
		await serverAnswers(serverRow(true));
		openCard();
		expectAttached();
		edit(6, 7, 'a');
		expectOrphaned();
		edit(6, 7, 'i');
		expectAttached();
	});

	it('keeps a refusal through edits, the quote restored and a save, and drops it when the suggestion is decided', async () => {
		await serverAnswers(serverRow(false));
		openCard();
		await refuseAccept();
		edit(START.length, START.length, ' ');
		edit(6, 7, 'a');
		edit(6, 7, 'i');
		mocks.persistPositions.mockResolvedValue(undefined);
		await flushPositions(ARTIFACT, get(screenText)); flushSync();
		expectOrphaned();
		expect(alert()).toBe(STALE);
		mocks.decideSuggestion.mockResolvedValue({ comment: { id: 'comment-1' } });
		mocks.fetchComments.mockResolvedValueOnce({ comments: [serverRow(true, { resolved: 1 })], feedback: {} });
		host.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!.click();
		await vi.waitFor(() => expect(get(suggestionRefusals)).toEqual({ [ARTIFACT]: {} }));
		expect(alert()).toBeNull();
	});

	it.each([
		{ failure: 'the save before it', setup: () => mocks.flushDraft.mockResolvedValue(false), message: "Couldn't save the document before accepting this suggestion" },
		{ failure: 'no answer from the server', setup: () => mocks.decideSuggestion.mockRejectedValue(new TypeError('Failed to fetch')), message: 'Failed to fetch' },
		{ failure: 'a refusal that is not about the text', setup: () => mocks.decideSuggestion.mockRejectedValue(new SuggestionRefused('Only the owner can decide', 400)), message: 'Only the owner can decide' },
	])('leaves a card attached after an accept that failed on $failure, and keeps the reason', async ({ setup, message }) => {
		await serverAnswers(serverRow(false));
		openCard();
		mocks.flushDraft.mockResolvedValue(true);
		setup();
		accept().click();
		await vi.waitFor(() => expect(alert()).toBe(message));
		expectAttached();
		expect(get(suggestionRefusals)).toEqual({});
		edit(6, 7, 'a');
		expectOrphaned();
		edit(6, 7, 'i');
		expectAttached();
		expect(alert()).toBe(message);
	});

	it('keeps a failed reject on a card the screen orphaned when the words come back', async () => {
		await serverAnswers(serverRow(false));
		openCard();
		edit(6, 7, 'a');
		expectOrphaned();
		mocks.decideSuggestion.mockRejectedValue(new Error('Network down'));
		host.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!.click();
		await vi.waitFor(() => expect(alert()).toBe('Network down'));
		// A failed reject says nothing about the text, so restoring the words does not answer it.
		edit(6, 7, 'i');
		expectAttached();
		expect(alert()).toBe('Network down');
	});
});

// The transition table. The text on screen places every comment; the one other input is a 409 refusal of that
// card's Accept, which shows it orphaned with the server's reason until it is decided or the surface resets.
// A load's own `orphaned` is never an input. Each row drives the real stores and surface and states what the user sees.
describe('what the user sees for each way a suggestion card is attached or orphaned', () => {
	const ATTACHED = { section: 'Open comments', accept: true, highlighted: true, jump: { from: 4, to: 9 }, order: ['comment-1', 'located'] };
	const SHIFTED = { ...ATTACHED, jump: { from: 8, to: 13 } };
	const ORPHANED = { section: 'Text changed', accept: false, highlighted: false, jump: null, order: ['located', 'comment-1'] };
	// A comment further down whose text never changes, to sort against.
	const located = serverRow(false, { id: 'located', anchor_text: 'fox', from_off: 16, to_off: 19, replacement: null });
	async function load(orphaned: boolean) {
		await serverAnswers(serverRow(orphaned), located);
	}
	const breakQuote = () => edit(6, 7, 'a');
	const breakAndRestore = () => { edit(6, 7, 'a'); edit(6, 7, 'i'); };
	// "So. " moves every offset after it; the quote's own text is untouched.
	const editBefore = () => edit(0, 0, 'So. ');
	const editAfter = () => edit(START.length, START.length, ' ');
	// The viewer replacing the text (a reload or an accepted suggestion) is not the user's edit: nothing is remapped.
	const programmaticSync = () => { screenText.set(`${START} More.`); flushSync(); };
	const refusedThen = (next: () => void | Promise<unknown>) => async () => { await refuseAccept(); await next(); };
	const failedReject = async () => {
		mocks.decideSuggestion.mockRejectedValue(new Error('Network down'));
		host.querySelector<HTMLButtonElement>('.ct-suggestion-actions .ct-link')!.click();
		await vi.waitFor(() => expect(alert()).toBe('Network down'));
	};
	const typeAndSave = async () => {
		editAfter();
		// The save writes the text and the moved positions; neither reloads comments (no change frame follows a positions write).
		mocks.persistPositions.mockResolvedValue(undefined);
		await flushPositions(ARTIFACT, get(screenText));
		flushSync();
	};

	it.each([
		{ row: 'attached', server: false, drive: () => {}, sees: ATTACHED, alert: null },
		{ row: 'server-orphaned, quote still on screen', server: true, drive: () => {}, sees: ATTACHED, alert: null },
		{ row: 'screen-orphaned, server placed it', server: false, drive: breakQuote, sees: ORPHANED, alert: null },
		{ row: 'orphaned by both', server: false, drive: async () => { breakQuote(); await load(true); }, sees: ORPHANED, alert: null },
		{ row: 'server-orphaned, then an edit before the quote', server: true, drive: editBefore, sees: SHIFTED, alert: null },
		{ row: 'server-orphaned, then an edit after the quote', server: true, drive: editAfter, sees: ATTACHED, alert: null },
		{ row: 'server-orphaned, then an edit that breaks the quote', server: true, drive: breakQuote, sees: ORPHANED, alert: null },
		{ row: 'server-orphaned, then the quote broken and restored exactly', server: true, drive: breakAndRestore, sees: ATTACHED, alert: null },
		{ row: 'server-orphaned, then a programmatic sync', server: true, drive: programmaticSync, sees: ATTACHED, alert: null },
		{ row: 'unsaved typing, then a load that reports it orphaned', server: false, drive: async () => { editAfter(); await load(true); }, sees: ATTACHED, alert: null },
		{ row: 'quote restored, then a load that reports it orphaned', server: false, drive: async () => { breakAndRestore(); await load(true); }, sees: ATTACHED, alert: null },
		{ row: 'refused accept', server: false, drive: refuseAccept, sees: ORPHANED, alert: STALE },
		{ row: 'refused accept, then a load that succeeds', server: false, drive: refusedThen(() => load(false)), sees: ORPHANED, alert: STALE },
		{ row: 'refused accept, then a load that fails', server: false, drive: refusedThen(() => anotherLoad('fails')), sees: ORPHANED, alert: STALE },
		{ row: 'refused accept, then an edit before the quote', server: false, drive: refusedThen(editBefore), sees: ORPHANED, alert: STALE },
		{ row: 'refused accept, then an edit after the quote', server: false, drive: refusedThen(editAfter), sees: ORPHANED, alert: STALE },
		{ row: 'refused accept, then an edit that breaks the quote', server: false, drive: refusedThen(breakQuote), sees: ORPHANED, alert: STALE },
		{ row: 'refused accept, then the quote broken and restored exactly', server: false, drive: refusedThen(breakAndRestore), sees: ORPHANED, alert: STALE },
		{ row: 'refused accept, then typing and a save', server: false, drive: refusedThen(typeAndSave), sees: ORPHANED, alert: STALE },
		{ row: 'refused accept, then a failed reject (its error shows over the refusal)', server: false, drive: refusedThen(failedReject), sees: ORPHANED, alert: 'Network down' },
		{ row: 'server-orphaned, then a failed reject', server: true, drive: failedReject, sees: ATTACHED, alert: 'Network down' },
	])('$row', async ({ server, drive, sees, alert: expected }) => {
		await load(server);
		openCard();
		await drive();
		expect(group()).toBe(sees.section);
		expect(accept().disabled).toBe(!sees.accept);
		expect(alert()).toBe(expected);
		// The mark and the jump are DocViewer's: these are the two rules it calls, on the comments it would hold.
		const card = listed().find((comment) => comment.id === 'comment-1')!;
		expect(canvasCommentHighlights(listed(), get(screenText), null, undefined).some((mark) => mark.id === 'comment-1')).toBe(sees.highlighted);
		expect(canvasCommentRange(card, get(screenText))).toEqual(sees.jump);
		// The drawer lists every card in document order.
		surface.set({ kind: 'list' }); flushSync();
		expect([...host.querySelectorAll('[data-comment-id]')].map((item) => item.getAttribute('data-comment-id'))).toEqual(sees.order);
	});
});
