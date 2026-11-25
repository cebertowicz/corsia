import { afterEach, describe, expect, it, vi } from 'vitest';
import { SuggestionRefused } from '$lib/api/commentSuggestions';
import { createViewerSuggestionHistory } from './docViewerSuggestionHistory.svelte';

const mocks = vi.hoisted(() => ({ flush: vi.fn(), seed: vi.fn(), transition: vi.fn(), comments: vi.fn(), toast: vi.fn() }));
vi.mock('$lib/stores/docSaveQueue', () => ({ flushDraft: mocks.flush, seedDraft: mocks.seed }));
vi.mock('$lib/stores/docComments', () => ({ loadComments: mocks.comments }));
vi.mock('$lib/api/commentSuggestions', async (importActual) => ({
	...await importActual<typeof import('$lib/api/commentSuggestions')>(),
	transitionSuggestionHistory: mocks.transition,
}));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: mocks.toast } }));

const target = { instanceId: 'tab', path: '/doc.md', name: 'doc.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' };
const entry = { eventId: 'event', ids: ['s1'], direction: 'undo' as const };

describe('viewer suggestion history', () => {
	it('holds suggestion controls until the server result and comments have settled', async () => {
		let finish!: () => void;
		mocks.flush.mockResolvedValue(true);
		mocks.transition.mockResolvedValue({ text: 'old', revision: 'revision' });
		mocks.comments.mockImplementation(() => new Promise<void>(resolve => { finish = resolve; }));
		const setBusy = vi.fn();
		const history = createViewerSuggestionHistory({ target: () => target, artifact: () => 'artifact', unavailable: () => false, isLive: () => false, accept: vi.fn(), setText: vi.fn(), setBusy, reload: vi.fn() });
		const result = history.change(entry, 'new', 'old');
		expect(history.busy).toBe(true);
		await vi.waitFor(() => expect(mocks.comments).toHaveBeenCalled());
		expect(history.busy).toBe(true);
		finish();
		expect(await result).toBe(true);
		expect(history.busy).toBe(false);
		expect(setBusy.mock.calls).toEqual([[true], [false]]);
		expect(mocks.seed).toHaveBeenCalledWith(target, 'old', 'revision');
	});

	const FIXED = 'Couldn’t change the suggestion. Reload and try again.';
	function setup(overrides: { target?: () => typeof target | null } = {}) {
		const reload = vi.fn();
		const history = createViewerSuggestionHistory({ target: overrides.target ?? (() => target), artifact: () => 'artifact', unavailable: () => false, isLive: () => false, accept: vi.fn(), setText: vi.fn(), setBusy: vi.fn(), reload });
		return { history, reload };
	}

	afterEach(() => {
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
		for (const mock of Object.values(mocks)) mock.mockReset();
	});

	it('sends the full WebCrypto SHA-256 of the text, also where crypto.subtle is absent', async () => {
		mocks.flush.mockResolvedValue(true);
		mocks.transition.mockResolvedValue({ text: 'old', revision: 'revision' });
		mocks.comments.mockResolvedValue(undefined);
		const expected = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode('new text, é')))).map(b => b.toString(16).padStart(2, '0')).join('');
		expect(expected).toHaveLength(64);
		const { history } = setup();
		expect(await history.change(entry, 'new text, é', 'old')).toBe(true);
		expect(mocks.transition.mock.calls[0][4]).toBe(expected);
		mocks.transition.mockClear();
		vi.stubGlobal('crypto', { ...globalThis.crypto, subtle: undefined });
		expect(await history.change(entry, 'new text, é', 'old')).toBe(true);
		expect(mocks.transition.mock.calls[0][4]).toBe(expected);
		expect(mocks.toast).not.toHaveBeenCalled();
	});

	it.each([
		['a missing result', () => mocks.transition.mockResolvedValue({}), () => undefined],
		['a runtime TypeError', () => mocks.transition.mockRejectedValue(new TypeError("Cannot read properties of undefined (reading 'digest')")), () => undefined],
		['a network error', () => mocks.transition.mockRejectedValue(new TypeError('Failed to fetch')), () => undefined],
	])('shows the written message, not raw text, for %s', async (_name, arrange) => {
		mocks.flush.mockResolvedValue(true);
		arrange();
		const { history, reload } = setup();
		expect(await history.change(entry, 'new', 'old')).toBe(false);
		expect(mocks.toast).toHaveBeenCalledOnce();
		expect(mocks.toast).toHaveBeenCalledWith(FIXED, 'error');
		expect(reload).toHaveBeenCalledOnce();
	});

	it('keeps the server’s own refusal message', async () => {
		mocks.flush.mockResolvedValue(true);
		mocks.transition.mockRejectedValue(new SuggestionRefused('This suggestion was decided somewhere else.', 409, 'decided'));
		const { history, reload } = setup();
		expect(await history.change(entry, 'new', 'old')).toBe(false);
		expect(mocks.toast).toHaveBeenCalledWith('This suggestion was decided somewhere else.', 'error');
		expect(reload).toHaveBeenCalledOnce();
	});

	it('reloads quietly when only the comments load fails after the server applied the change', async () => {
		mocks.flush.mockResolvedValue(true);
		mocks.transition.mockResolvedValue({ text: 'old', revision: 'revision' });
		mocks.comments.mockRejectedValue(new TypeError('Failed to fetch'));
		const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
		const { history, reload } = setup();
		expect(await history.change(entry, 'new', 'old')).toBe(true);
		expect(mocks.seed).toHaveBeenCalledWith(target, 'old', 'revision');
		expect(mocks.toast).not.toHaveBeenCalled();
		expect(reload).toHaveBeenCalledOnce();
		expect(error).toHaveBeenCalledOnce();
	});
});
