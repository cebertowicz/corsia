import { flushSync } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DocViewerTarget } from '$lib/docViewerTarget';

const api = vi.hoisted(() => ({ decideSuggestion: vi.fn() }));
const comments = vi.hoisted(() => ({ loadComments: vi.fn() }));
const queue = vi.hoisted(() => ({ flushDraft: vi.fn(), seedDraft: vi.fn() }));

vi.mock('$lib/api/commentSuggestions', async (importOriginal) => ({ ...(await importOriginal<typeof import('$lib/api/commentSuggestions')>()), decideSuggestion: api.decideSuggestion }));
vi.mock('$lib/stores/docComments', () => comments);
vi.mock('$lib/stores/docSaveQueue', () => queue);

import { SuggestionRefused } from '$lib/api/commentSuggestions';
import { createSuggestionDecisions } from './docSuggestionDecisions.svelte';

const target: DocViewerTarget = { instanceId: 'tab1', path: 'a.md', name: 'a.md', projectId: null, threadId: null, artifactId: 'art1' };
const FALLBACK = 'Couldn’t accept this suggestion. Try again.';

function setup(callbacks: { onSuggestionAccepted?: (text: string) => void; onDocumentChanged?: () => void } = {}) {
	let decisions!: ReturnType<typeof createSuggestionDecisions>;
	const stop = $effect.root(() => {
		decisions = createSuggestionDecisions({ comments: () => [], artifactId: () => 'art1', target: () => target, open: () => true, ...callbacks });
	});
	flushSync();
	return { decisions, stop };
}

describe('a suggestion decision the server applied or refused', () => {
	let error: ReturnType<typeof vi.spyOn>;
	beforeEach(() => {
		error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
		queue.flushDraft.mockResolvedValue(true);
		comments.loadComments.mockResolvedValue(undefined);
		api.decideSuggestion.mockResolvedValue({ text: 'accepted text', revision: 'r2' });
	});
	afterEach(() => { vi.clearAllMocks(); error.mockRestore(); });

	it('shows no error and logs when this side’s follow-up throws after the server applied the accept', async () => {
		const onDocumentChanged = vi.fn();
		const failure = new Error('editor gone');
		const test = setup({ onSuggestionAccepted: () => { throw failure; }, onDocumentChanged });
		try {
			await test.decisions.decideOne('c1', 'accept');
			expect(test.decisions.outcome.errors.c1).toBe('');
			expect(error).toHaveBeenCalledWith('[suggestion decision] follow-up after the server applied it failed', { id: 'c1', decision: 'accept', allFromAuthor: false }, failure);
			expect(onDocumentChanged).toHaveBeenCalledOnce();
		} finally { test.stop(); }
	});

	it('reads the comments again, with no error, when loading them throws after the server applied the decision', async () => {
		comments.loadComments.mockRejectedValueOnce(new Error('offline')).mockResolvedValue(undefined);
		const onDocumentChanged = vi.fn();
		const test = setup({ onDocumentChanged });
		try {
			await test.decisions.decideOne('c1', 'reject');
			expect(test.decisions.outcome.errors.c1).toBe('');
			expect(onDocumentChanged).toHaveBeenCalledOnce();
		} finally { test.stop(); }
	});

	it('still shows the fallback and logs when the request never got an answer', async () => {
		const failure = new TypeError('Failed to fetch');
		api.decideSuggestion.mockRejectedValue(failure);
		const test = setup();
		try {
			await test.decisions.decideOne('c1', 'accept');
			expect(test.decisions.outcome.errors.c1).toBe(FALLBACK);
			expect(error).toHaveBeenCalledWith('[suggestion decision] failed', { id: 'c1', decision: 'accept' }, failure);
		} finally { test.stop(); }
	});

	it('logs an accept-all failure with its author scope', async () => {
		const failure = new TypeError('Failed to fetch');
		api.decideSuggestion.mockRejectedValue(failure);
		const test = setup();
		try {
			await test.decisions.acceptAllFrom('ana', 'c1');
			expect(test.decisions.outcome.acceptAllError).toBe('Couldn’t accept the suggestions. Try again.');
			expect(error).toHaveBeenCalledWith('[suggestion decision] failed', { id: 'c1', decision: 'accept', allFromAuthor: true }, failure);
		} finally { test.stop(); }
	});

	it('shows the server’s own text for a refusal and does not log', async () => {
		api.decideSuggestion.mockRejectedValue(new SuggestionRefused('Someone already decided this.', 409, 'decided'));
		const test = setup();
		try {
			await test.decisions.decideOne('c1', 'accept');
			expect(test.decisions.outcome.errors.c1).toBe('Someone already decided this.');
			expect(error).not.toHaveBeenCalled();
			expect(comments.loadComments).toHaveBeenCalledWith('art1');
		} finally { test.stop(); }
	});
});
