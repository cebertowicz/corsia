import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { fromStore, get, writable } from 'svelte/store';

const mocks = vi.hoisted(() => ({
	decideSuggestion: vi.fn(), flushDraft: vi.fn(), seedDraft: vi.fn(), loadComments: vi.fn(),
}));
vi.mock('$lib/api/commentSuggestions', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/api/commentSuggestions')>(), decideSuggestion: mocks.decideSuggestion,
}));
vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: mocks.flushDraft, seedDraft: mocks.seedDraft }));
vi.mock('$lib/stores/docComments', () => ({ loadComments: mocks.loadComments }));

import DocCommentSurface from './DocCommentSurface.svelte';
import { SuggestionRefused } from '$lib/api/commentSuggestions';
import { suggestionRefusals } from '$lib/stores/suggestionRefusals';

const first = { instanceId: 'tab-1', path: 'notes.md', name: 'Notes', projectId: 'project-1', threadId: 'thread-1', artifactId: 'artifact-1' };
const second = { ...first, instanceId: 'tab-2', path: 'other.md', name: 'Other', artifactId: 'artifact-2' };
const suggestion = {
	id: 'comment-1', anchor: 'old text', author: 'agent-1', authorKind: 'agent' as const,
	text: 'Prefer this wording', at: 'now', resolved: false, replacement: 'new text', replies: [],
};
const secondSuggestion = { ...suggestion, id: 'comment-2', anchor: 'other text' };

// The viewer is one component for every tab, so the document and the surface can change under a decision still running.
const shown = writable(first);
const surface = writable<{ kind: 'list' } | null>({ kind: 'list' });
const shownNow = fromStore(shown), surfaceNow = fromStore(surface);
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const onSuggestionAccepted = vi.fn(), onSuggestionBusy = vi.fn();

function render() {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(DocCommentSurface, {
		target: host,
		props: Object.defineProperties({
			state: { kind: 'list' }, anchor: document.createElement('button'), narrow: false,
			comments: [suggestion, secondSuggestion], focusedId: null, draft: null, onClose: vi.fn(), restoreFocus: vi.fn(),
			onFocus: vi.fn(), onSubmit: vi.fn(), onReply: vi.fn(), onResolve: vi.fn(), onAgree: vi.fn(),
			target: first, artifactId: first.artifactId, onSuggestionAccepted, onSuggestionBusy,
		}, Object.getOwnPropertyDescriptors({
			get state() { return surfaceNow.current; },
			get target() { return shownNow.current; },
			get artifactId() { return shownNow.current.artifactId; },
		})),
	});
	flushSync();
}
/** A decision whose request the test settles by hand, after `meanwhile` has happened. */
function pendingDecision() {
	let succeed!: (result: unknown) => void, fail!: (error: Error) => void;
	mocks.flushDraft.mockResolvedValue(true);
	mocks.loadComments.mockResolvedValue(undefined);
	mocks.decideSuggestion.mockImplementation(() => new Promise((resolve, reject) => { succeed = resolve; fail = reject; }));
	return { succeed: (result: unknown) => succeed(result), fail: (error: Error) => fail(error) };
}
async function settled() {
	await vi.waitFor(() => expect(onSuggestionBusy).toHaveBeenLastCalledWith(false));
	await new Promise((done) => setTimeout(done));
	flushSync();
}
function reopen() {
	surface.set(null); flushSync();
	surface.set({ kind: 'list' }); flushSync();
}
// What the viewer does on a tab switch: it closes the surface and shows the other document; the user opens its comments.
function switchDocument() {
	surface.set(null); shown.set(second); flushSync();
	surface.set({ kind: 'list' }); flushSync();
}
const meanwhile = [
	{ what: 'nothing changed', act: () => {}, sameOpening: true, sameDocument: true },
	{ what: 'the surface closed and reopened', act: reopen, sameOpening: false, sameDocument: true },
	{ what: 'the document changed', act: switchDocument, sameOpening: false, sameDocument: false },
];

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
	shown.set(first); surface.set({ kind: 'list' }); suggestionRefusals.set({});
	vi.resetAllMocks();
});

describe('a decision that settles after the surface moved on', () => {
	it.each(meanwhile)('accept-all fails after $what: shown only on the opening that started it', async ({ act, sameOpening }) => {
		const decision = pendingDecision();
		render();
		host.querySelector<HTMLButtonElement>('.suggestion-status button')!.click();
		await vi.waitFor(() => expect(mocks.decideSuggestion).toHaveBeenCalled());
		act();
		decision.fail(new Error('The document could not be saved'));
		await settled();
		expect(host.querySelector('.suggestion-error')?.textContent ?? null).toBe(sameOpening ? 'The document could not be saved' : null);
	});

	it.each(meanwhile)('a reject fails after $what: shown only on the opening that started it', async ({ act, sameOpening }) => {
		const decision = pendingDecision();
		render();
		host.querySelector<HTMLButtonElement>('[data-comment-id="comment-1"] .ct-suggestion-actions .ct-link')!.click();
		await vi.waitFor(() => expect(mocks.decideSuggestion).toHaveBeenCalled());
		act();
		decision.fail(new Error('Network down'));
		await settled();
		expect(host.querySelector('[role="alert"]')?.textContent ?? null).toBe(sameOpening ? 'Network down' : null);
	});

	it.each(meanwhile)('an accept refused with 409 after $what: recorded only on the opening that started it', async ({ act, sameOpening }) => {
		const decision = pendingDecision();
		render();
		host.querySelector<HTMLButtonElement>('[data-comment-id="comment-1"] .ct-suggestion-accept')!.click();
		await vi.waitFor(() => expect(mocks.decideSuggestion).toHaveBeenCalled());
		act();
		decision.fail(new SuggestionRefused('The text changed on the server', 409, 'stale'));
		await settled();
		expect(get(suggestionRefusals)).toEqual(sameOpening ? { 'artifact-1': { 'comment-1': 'The text changed on the server' } } : {});
	});

	it.each(meanwhile)('accept-all succeeds after $what: its text goes to its own document', async ({ act, sameOpening, sameDocument }) => {
		const decision = pendingDecision();
		render();
		host.querySelector<HTMLButtonElement>('.suggestion-status button')!.click();
		await vi.waitFor(() => expect(mocks.decideSuggestion).toHaveBeenCalled());
		act();
		decision.succeed({ accepted: ['comment-1'], skipped: [{ id: 'comment-2', reason: 'overlaps an earlier suggestion' }], text: 'accepted document', revision: 'r3' });
		await settled();
		// The draft and the comments that are refreshed are the first document's, whichever one is shown now.
		expect(mocks.flushDraft).toHaveBeenCalledExactlyOnceWith(first);
		expect(mocks.seedDraft).toHaveBeenCalledExactlyOnceWith(first, 'accepted document', 'r3');
		expect(mocks.loadComments).toHaveBeenCalledExactlyOnceWith('artifact-1');
		// Closing the surface does not change which document is on screen; switching tabs does.
		expect(onSuggestionAccepted.mock.calls).toEqual(sameDocument ? [['accepted document']] : []);
		expect(host.textContent?.includes('1 skipped')).toBe(sameOpening);
	});

	it.each(meanwhile.slice(1))('a skipped list from a finished accept-all is gone after $what', async ({ act }) => {
		const decision = pendingDecision();
		render();
		host.querySelector<HTMLButtonElement>('.suggestion-status button')!.click();
		await vi.waitFor(() => expect(mocks.decideSuggestion).toHaveBeenCalled());
		decision.succeed({ accepted: ['comment-1'], skipped: [{ id: 'comment-2', reason: 'overlaps an earlier suggestion' }], text: 'accepted document', revision: 'r3' });
		await settled();
		expect(host.textContent?.includes('1 skipped')).toBe(true);
		// Its reasons are about the first document's suggestions, so they do not stay on what replaced it.
		act();
		expect(host.textContent?.includes('1 skipped')).toBe(false);
	});

	it.each(meanwhile.slice(1))('an accept-all still running after $what is not shown as running, but still blocks another', async ({ act }) => {
		const decision = pendingDecision();
		render();
		const acceptAll = () => host.querySelector<HTMLButtonElement>('.suggestion-status button')!;
		acceptAll().click();
		await vi.waitFor(() => expect(mocks.decideSuggestion).toHaveBeenCalled());
		flushSync();
		expect(acceptAll().textContent).toBe('Accepting…');
		act();
		// The request is the first document's; one at a time, so the button stays disabled wherever it is shown.
		expect(acceptAll().textContent).toBe('Accept all from agent-1');
		expect(acceptAll().disabled).toBe(true);
		decision.succeed({ accepted: ['comment-1', 'comment-2'], skipped: [], text: 'accepted document', revision: 'r3' });
		await settled();
		expect(acceptAll().disabled).toBe(false);
	});
});
